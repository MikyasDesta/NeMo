# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# ##########################################################################


import torch
from hydra.utils import instantiate
from omegaconf import DictConfig, OmegaConf
from pytorch_lightning import Trainer
from pytorch_lightning.loggers import LoggerCollection, TensorBoardLogger

from nemo.collections.tts.helpers.helpers import plot_alignment_to_numpy
from nemo.collections.tts.losses.radttsloss import AttentionBinarizationLoss, RADTTSLoss
from nemo.collections.tts.models.base import SpectrogramGenerator
from nemo.collections.tts.torch.tts_tokenizers import BaseTokenizer
from nemo.core.classes import Exportable
from nemo.core.classes.common import PretrainedModelInfo, typecheck
from nemo.core.neural_types.elements import (
    Index,
    LengthsType,
    MelSpectrogramType,
    ProbsType,
    RegressionValuesType,
    TokenDurationType,
    TokenIndex,
    TokenLogDurationType,
)
from nemo.core.neural_types.neural_type import NeuralType
from nemo.core.optim.radam import RAdam
from nemo.utils import logging


class RadTTSModel(SpectrogramGenerator, Exportable):
    def __init__(self, cfg: DictConfig, trainer: Trainer = None):
        if isinstance(cfg, dict):
            cfg = OmegaConf.create(cfg)

        self._setup_tokenizer(cfg.validation_ds.dataset)

        assert self.tokenizer is not None

        self.tokenizer_pad = self.tokenizer.pad
        self.tokenizer_unk = self.tokenizer.oov

        self.text_tokenizer_pad_id = None
        self.tokens = None

        super().__init__(cfg=cfg, trainer=trainer)
        self.feat_loss_weight = 1.0
        self.model_config = cfg.modelConfig
        self.train_config = cfg.trainerConfig
        self.optim = cfg.optim
        self.criterion = RADTTSLoss(
            self.train_config.sigma,
            self.model_config.n_group_size,
            self.model_config.dur_model_config,
            self.model_config.f0_model_config,
            self.model_config.energy_model_config,
            vpred_model_config=self.model_config.v_model_config,
            loss_weights=self.train_config.loss_weights,
        )

        self.attention_kl_loss = AttentionBinarizationLoss()
        self.model = instantiate(cfg.modelConfig)
        self._parser = None
        self._tb_logger = None
        self.cfg = cfg
        self.log_train_images = False

        self.normalizer = None
        self.text_normalizer_call = None
        self.text_normalizer_call_kwargs = {}
        self._setup_normalizer(cfg)

    def batch_dict(self, batch_data):
        if len(batch_data) < 14:
            spk_id = torch.tensor([0] * (batch_data[3]).size(0)).cuda().to(self.device)
        else:
            spk_id = batch_data[13]
        batch_data_dict = {
            "audio": batch_data[0],
            "audio_lens": batch_data[1],
            "text": batch_data[2],
            "text_lens": batch_data[3],
            "log_mel": batch_data[4],
            "log_mel_lens": batch_data[5],
            "align_prior_matrix": batch_data[6],
            "pitch": batch_data[7],
            "pitch_lens": batch_data[8],
            "voiced_mask": batch_data[9],
            "p_voiced": batch_data[10],
            "energy": batch_data[11],
            "energy_lens": batch_data[12],
            "speaker_id": spk_id,
        }
        return batch_data_dict

    def training_step(self, batch, batch_idx):
        batch = self.batch_dict(batch)
        mel = batch['log_mel']
        speaker_ids = batch['speaker_id']
        text = batch['text']
        in_lens = batch['text_lens']
        out_lens = batch['log_mel_lens']
        attn_prior = batch['align_prior_matrix']
        f0 = batch['pitch']
        voiced_mask = batch['voiced_mask']
        p_voiced = batch['p_voiced']
        energy_avg = batch['energy']

        if (
            self.train_config.binarization_start_iter >= 0
            and self.global_step >= self.train_config.binarization_start_iter
        ):
            # binarization training phase
            binarize = True
        else:
            # no binarization, soft-only
            binarize = False

        outputs = self.model(
            mel,
            speaker_ids,
            text,
            in_lens,
            out_lens,
            binarize_attention=binarize,
            attn_prior=attn_prior,
            f0=f0,
            energy_avg=energy_avg,
            voiced_mask=voiced_mask,
            p_voiced=p_voiced,
        )
        loss_outputs = self.criterion(outputs, in_lens, out_lens)

        loss = None
        for k, (v, w) in loss_outputs.items():
            if w > 0:
                loss = v * w if loss is None else loss + v * w

        if binarize and self.global_step >= self.train_config.kl_loss_start_iter:
            binarization_loss = self.attention_kl_loss(outputs['attn'], outputs['attn_soft'])
            loss += binarization_loss
        else:
            binarization_loss = torch.zeros_like(loss)
        loss_outputs['binarization_loss'] = (binarization_loss, 1.0)

        for k, (v, w) in loss_outputs.items():
            self.log("train/" + k, loss_outputs[k][0])

        return {'loss': loss}

    def validation_step(self, batch, batch_idx):
        # print("batch", batch)
        batch = self.batch_dict(batch)
        speaker_ids = batch['speaker_id']
        text = batch['text']
        in_lens = batch['text_lens']
        out_lens = batch['log_mel_lens']
        attn_prior = batch['align_prior_matrix']
        f0 = batch['pitch']
        voiced_mask = batch['voiced_mask']
        p_voiced = batch['p_voiced']
        energy_avg = batch['energy']
        mel = batch['log_mel']
        if (
            self.train_config.binarization_start_iter >= 0
            and self.global_step >= self.train_config.binarization_start_iter
        ):
            # binarization training phase
            binarize = True
        else:
            # no binarization, soft-only
            binarize = False
        outputs = self.model(
            mel,
            speaker_ids,
            text,
            in_lens,
            out_lens,
            binarize_attention=True,
            attn_prior=attn_prior,
            f0=f0,
            energy_avg=energy_avg,
            voiced_mask=voiced_mask,
            p_voiced=p_voiced,
        )
        loss_outputs = self.criterion(outputs, in_lens, out_lens)

        loss = None
        for k, (v, w) in loss_outputs.items():
            if w > 0:
                loss = v * w if loss is None else loss + v * w

        if (
            binarize
            and self.train_config.kl_loss_start_iter >= 0
            and self.global_step >= self.train_config.kl_loss_start_iter
        ):
            binarization_loss = self.attention_kl_loss(outputs['attn'], outputs['attn_soft'])
            loss += binarization_loss
        else:
            binarization_loss = torch.zeros_like(loss)
        loss_outputs['binarization_loss'] = binarization_loss

        return {
            "loss_outputs": loss_outputs,
            "attn": outputs["attn"] if batch_idx == 0 else None,
            "attn_soft": outputs["attn_soft"] if batch_idx == 0 else None,
            "audiopaths": "audio_1" if batch_idx == 0 else None,
        }

    def validation_epoch_end(self, outputs):

        loss_outputs = outputs[0]["loss_outputs"]

        for k, v in loss_outputs.items():
            if k != "binarization_loss":
                self.log("val/" + k, loss_outputs[k][0])

        attn = outputs[0]["attn"]
        attn_soft = outputs[0]["attn_soft"]

        self.tb_logger.add_image(
            'attention_weights_mas',
            plot_alignment_to_numpy(attn[0, 0].data.cpu().numpy().T, title="audio"),
            self.global_step,
            dataformats='HWC',
        )

        self.tb_logger.add_image(
            'attention_weights',
            plot_alignment_to_numpy(attn_soft[0, 0].data.cpu().numpy().T, title="audio"),
            self.global_step,
            dataformats='HWC',
        )
        self.log_train_images = True

    def configure_optimizers(self):
        logging.info("Initializing %s optimizer" % (self.optim.name))
        if len(self.train_config.finetune_layers):
            for name, param in model.named_parameters():
                if any([l in name for l in self.train_config.finetune_layers]):  # short list hack
                    logging.info("Fine-tuning parameter", name)
                    param.requires_grad = True
                else:
                    param.requires_grad = False
        if self.optim.name == 'Adam':
            optimizer = torch.optim.Adam(
                self.model.parameters(), lr=self.optim.lr, weight_decay=self.optim.weight_decay
            )
        elif self.optim.name == 'RAdam':
            optimizer = RAdam(self.model.parameters(), lr=self.optim.lr, weight_decay=self.optim.weight_decay)
        else:
            logging.info("Unrecognized optimizer %s!" % (self.optim.name))
            exit(1)

        return optimizer

    @staticmethod
    def _loader(cfg):
        try:
            _ = cfg.dataset.manifest_filepath
        except omegaconf.errors.MissingMandatoryValue:
            logging.warning("manifest_filepath was skipped. No dataset for this model.")
            return None

        dataset = instantiate(cfg.dataset)
        return torch.utils.data.DataLoader(  # noqa
            dataset=dataset, collate_fn=dataset.collate_fn, **cfg.dataloader_params,
        )

    def setup_training_data(self, cfg):
        self._train_dl = self._loader(cfg)

    def setup_validation_data(self, cfg):
        self._validation_dl = self._loader(cfg)

    def setup_test_data(self, cfg):
        """Omitted."""
        pass

    @typecheck(
        input_types={
            "tokens": NeuralType(('B', 'T_text'), TokenIndex(), optional=True),
            "speaker": NeuralType(('B'), Index(), optional=True),
            "sigma": NeuralType(optional=True),
        },
        output_types={"spect": NeuralType(('B', 'D', 'T_spec'), MelSpectrogramType()),},
    )
    def generate_spectrogram(self, tokens: 'torch.tensor', speaker: int = 0, sigma: float = 1.0) -> torch.tensor:
        self.eval()
        # s = [0]
        if self.training:
            logging.warning("generate_spectrogram() is meant to be called in eval mode.")
        speaker = torch.tensor([speaker]).long().cuda().to(self.device)
        outputs = self.model.infer(speaker, tokens, sigma=sigma)

        spect = outputs['mel']
        return spect

    @property
    def parser(self):
        if self._parser is not None:
            return self._parser
        return self._parser

    def _setup_normalizer(self, cfg):
        if "text_normalizer" in cfg:
            normalizer_kwargs = {}

            if "whitelist" in cfg.text_normalizer:
                normalizer_kwargs["whitelist"] = self.register_artifact(
                    'text_normalizer.whitelist', cfg.text_normalizer.whitelist
                )

            self.normalizer = instantiate(cfg.text_normalizer, **normalizer_kwargs)
            self.text_normalizer_call = self.normalizer.normalize
            if "text_normalizer_call_kwargs" in cfg:
                self.text_normalizer_call_kwargs = cfg.text_normalizer_call_kwargs

    def _setup_tokenizer(self, cfg):
        text_tokenizer_kwargs = {}
        if "g2p" in cfg.text_tokenizer:
            g2p_kwargs = {}

            if "phoneme_dict" in cfg.text_tokenizer.g2p:
                g2p_kwargs["phoneme_dict"] = self.register_artifact(
                    'text_tokenizer.g2p.phoneme_dict', cfg.text_tokenizer.g2p.phoneme_dict,
                )

            if "heteronyms" in cfg.text_tokenizer.g2p:
                g2p_kwargs["heteronyms"] = self.register_artifact(
                    'text_tokenizer.g2p.heteronyms', cfg.text_tokenizer.g2p.heteronyms,
                )
            if "adlr_symbol_id_mapper" in cfg.text_tokenizer.g2p:
                adlr_symbol_to_id = self.register_artifact(
                    'text_tokenizer.g2p.adlr_symbol_id_mapper', cfg.text_tokenizer.g2p.adlr_symbol_id_mapper,
                )
            text_tokenizer_kwargs["g2p"] = instantiate(cfg.text_tokenizer.g2p, **g2p_kwargs)

        self.tokenizer = instantiate(cfg.text_tokenizer, **text_tokenizer_kwargs)
        if isinstance(self.tokenizer, BaseTokenizer):
            self.text_tokenizer_pad_id = self.tokenizer.pad
            self.tokens = self.tokenizer.tokens
        else:
            if text_tokenizer_pad_id is None:
                raise ValueError(f"text_tokenizer_pad_id must be specified if text_tokenizer is not BaseTokenizer")

            if tokens is None:
                raise ValueError(f"tokens must be specified if text_tokenizer is not BaseTokenizer")

            self.text_tokenizer_pad_id = text_tokenizer_pad_id
            self.tokens = tokens

    def parse(self, text: str, normalize=False) -> torch.Tensor:
        if self.training:
            logging.warning("parse() is meant to be called in eval mode.")
        if normalize and self.text_normalizer_call is not None:
            text = self.text_normalizer_call(text, **self.text_normalizer_call_kwargs)
        return torch.tensor(self.tokenizer(text)).long().unsqueeze(0).cuda().to(self.device)

    @property
    def tb_logger(self):
        if self._tb_logger is None:
            if self.logger is None and self.logger.experiment is None:
                return None
            tb_logger = self.logger.experiment
            if isinstance(self.logger, LoggerCollection):
                for logger in self.logger:
                    if isinstance(logger, TensorBoardLogger):
                        tb_logger = logger.experiment
                        break
            self._tb_logger = tb_logger
        return self._tb_logger

    def get_export_subnet(self, subnet=None):
        return self.model.get_export_subnet(subnet)
