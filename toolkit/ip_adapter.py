import torch
import sys

from PIL import Image
from torch.nn import Parameter
from transformers import CLIPImageProcessor, CLIPVisionModelWithProjection

from toolkit.models.clip_pre_processor import CLIPImagePreProcessor
from toolkit.paths import REPOS_ROOT
from toolkit.saving import load_ip_adapter_model
from toolkit.train_tools import get_torch_dtype

sys.path.append(REPOS_ROOT)
from typing import TYPE_CHECKING, Union, Iterator, Mapping, Any, Tuple, List, Optional
from collections import OrderedDict
from ipadapter.ip_adapter.attention_processor import AttnProcessor, IPAttnProcessor, IPAttnProcessor2_0, \
    AttnProcessor2_0
from ipadapter.ip_adapter.ip_adapter import ImageProjModel
from ipadapter.ip_adapter.resampler import Resampler
from toolkit.config_modules import AdapterConfig
from toolkit.prompt_utils import PromptEmbeds
import weakref

if TYPE_CHECKING:
    from toolkit.stable_diffusion_model import StableDiffusion

from transformers import (
    CLIPImageProcessor,
    CLIPVisionModelWithProjection,
    CLIPVisionModel,
    AutoImageProcessor,
    ConvNextModel,
    ConvNextForImageClassification,
    ConvNextImageProcessor
)
from toolkit.models.size_agnostic_feature_encoder import SAFEImageProcessor, SAFEVisionModel

from transformers import ViTHybridImageProcessor, ViTHybridForImageClassification

from transformers import ViTFeatureExtractor, ViTForImageClassification

import torch.nn.functional as F


class CustomIPAttentionProcessor(IPAttnProcessor2_0):
    def __init__(self, hidden_size, cross_attention_dim, scale=1.0, num_tokens=4, adapter=None):
        super().__init__(hidden_size, cross_attention_dim, scale=scale, num_tokens=num_tokens)
        self.adapter_ref: weakref.ref = weakref.ref(adapter)

    def __call__(
            self,
            attn,
            hidden_states,
            encoder_hidden_states=None,
            attention_mask=None,
            temb=None,
    ):
        is_active = self.adapter_ref().is_active
        residual = hidden_states

        if attn.spatial_norm is not None:
            hidden_states = attn.spatial_norm(hidden_states, temb)

        input_ndim = hidden_states.ndim

        if input_ndim == 4:
            batch_size, channel, height, width = hidden_states.shape
            hidden_states = hidden_states.view(batch_size, channel, height * width).transpose(1, 2)

        batch_size, sequence_length, _ = (
            hidden_states.shape if encoder_hidden_states is None else encoder_hidden_states.shape
        )

        if attention_mask is not None:
            attention_mask = attn.prepare_attention_mask(attention_mask, sequence_length, batch_size)
            # scaled_dot_product_attention expects attention_mask shape to be
            # (batch, heads, source_length, target_length)
            attention_mask = attention_mask.view(batch_size, attn.heads, -1, attention_mask.shape[-1])

        if attn.group_norm is not None:
            hidden_states = attn.group_norm(hidden_states.transpose(1, 2)).transpose(1, 2)

        query = attn.to_q(hidden_states)

        # will be none if disabled
        if not is_active:
            ip_hidden_states = None
            if encoder_hidden_states is None:
                encoder_hidden_states = hidden_states
            elif attn.norm_cross:
                encoder_hidden_states = attn.norm_encoder_hidden_states(encoder_hidden_states)
        else:
            # get encoder_hidden_states, ip_hidden_states
            end_pos = encoder_hidden_states.shape[1] - self.num_tokens
            encoder_hidden_states, ip_hidden_states = (
                encoder_hidden_states[:, :end_pos, :],
                encoder_hidden_states[:, end_pos:, :],
            )
            if attn.norm_cross:
                encoder_hidden_states = attn.norm_encoder_hidden_states(encoder_hidden_states)

        key = attn.to_k(encoder_hidden_states)
        value = attn.to_v(encoder_hidden_states)

        inner_dim = key.shape[-1]
        head_dim = inner_dim // attn.heads

        query = query.view(batch_size, -1, attn.heads, head_dim).transpose(1, 2)

        key = key.view(batch_size, -1, attn.heads, head_dim).transpose(1, 2)
        value = value.view(batch_size, -1, attn.heads, head_dim).transpose(1, 2)

        # the output of sdp = (batch, num_heads, seq_len, head_dim)
        # TODO: add support for attn.scale when we move to Torch 2.1
        hidden_states = F.scaled_dot_product_attention(
            query, key, value, attn_mask=attention_mask, dropout_p=0.0, is_causal=False
        )

        hidden_states = hidden_states.transpose(1, 2).reshape(batch_size, -1, attn.heads * head_dim)
        hidden_states = hidden_states.to(query.dtype)

        # will be none if disabled
        if ip_hidden_states is not None:
            # for ip-adapter
            ip_key = self.to_k_ip(ip_hidden_states)
            ip_value = self.to_v_ip(ip_hidden_states)

            ip_key = ip_key.view(batch_size, -1, attn.heads, head_dim).transpose(1, 2)
            ip_value = ip_value.view(batch_size, -1, attn.heads, head_dim).transpose(1, 2)

            # the output of sdp = (batch, num_heads, seq_len, head_dim)
            # TODO: add support for attn.scale when we move to Torch 2.1
            ip_hidden_states = F.scaled_dot_product_attention(
                query, ip_key, ip_value, attn_mask=None, dropout_p=0.0, is_causal=False
            )

            ip_hidden_states = ip_hidden_states.transpose(1, 2).reshape(batch_size, -1, attn.heads * head_dim)
            ip_hidden_states = ip_hidden_states.to(query.dtype)

            hidden_states = hidden_states + self.scale * ip_hidden_states

        # linear proj
        hidden_states = attn.to_out[0](hidden_states)
        # dropout
        hidden_states = attn.to_out[1](hidden_states)

        if input_ndim == 4:
            hidden_states = hidden_states.transpose(-1, -2).reshape(batch_size, channel, height, width)

        if attn.residual_connection:
            hidden_states = hidden_states + residual

        hidden_states = hidden_states / attn.rescale_output_factor

        return hidden_states


# loosely based on # ref https://github.com/tencent-ailab/IP-Adapter/blob/main/tutorial_train.py
class IPAdapter(torch.nn.Module):
    """IP-Adapter"""

    def __init__(self, sd: 'StableDiffusion', adapter_config: 'AdapterConfig'):
        super().__init__()
        self.config = adapter_config
        self.sd_ref: weakref.ref = weakref.ref(sd)
        self.device = self.sd_ref().unet.device
        self.preprocessor: Optional[CLIPImagePreProcessor] = None
        self.input_size = 224
        if self.config.image_encoder_arch == 'clip' or self.config.image_encoder_arch == 'clip+':
            try:
                self.clip_image_processor = CLIPImageProcessor.from_pretrained(adapter_config.image_encoder_path)
            except EnvironmentError:
                self.clip_image_processor = CLIPImageProcessor()
            self.image_encoder = CLIPVisionModelWithProjection.from_pretrained(
                adapter_config.image_encoder_path,
                ignore_mismatched_sizes=True).to(self.device, dtype=get_torch_dtype(self.sd_ref().dtype))
        elif self.config.image_encoder_arch == 'siglip':
            from transformers import SiglipImageProcessor, SiglipVisionModel
            try:
                self.clip_image_processor = SiglipImageProcessor.from_pretrained(adapter_config.image_encoder_path)
            except EnvironmentError:
                self.clip_image_processor = SiglipImageProcessor()
            self.image_encoder = SiglipVisionModel.from_pretrained(
                adapter_config.image_encoder_path,
                ignore_mismatched_sizes=True).to(self.device, dtype=get_torch_dtype(self.sd_ref().dtype))
        elif self.config.image_encoder_arch == 'vit':
            try:
                self.clip_image_processor = ViTFeatureExtractor.from_pretrained(adapter_config.image_encoder_path)
            except EnvironmentError:
                self.clip_image_processor = ViTFeatureExtractor()
            self.image_encoder = ViTForImageClassification.from_pretrained(adapter_config.image_encoder_path).to(
                self.device, dtype=get_torch_dtype(self.sd_ref().dtype))
        elif self.config.image_encoder_arch == 'safe':
            try:
                self.clip_image_processor = SAFEImageProcessor.from_pretrained(adapter_config.image_encoder_path)
            except EnvironmentError:
                self.clip_image_processor = SAFEImageProcessor()
            self.image_encoder = SAFEVisionModel(
                in_channels=3,
                num_tokens=self.config.safe_tokens,
                num_vectors=sd.unet.config['cross_attention_dim'],
                reducer_channels=self.config.safe_reducer_channels,
                channels=self.config.safe_channels,
                downscale_factor=8
            ).to(self.device, dtype=get_torch_dtype(self.sd_ref().dtype))
        elif self.config.image_encoder_arch == 'convnext':
            try:
                self.clip_image_processor = ConvNextImageProcessor.from_pretrained(adapter_config.image_encoder_path)
            except EnvironmentError:
                print(f"could not load image processor from {adapter_config.image_encoder_path}")
                self.clip_image_processor = ConvNextImageProcessor(
                    size=320,
                    image_mean=[0.48145466, 0.4578275, 0.40821073],
                    image_std=[0.26862954, 0.26130258, 0.27577711],
                )
            self.image_encoder = ConvNextForImageClassification.from_pretrained(
                adapter_config.image_encoder_path,
                use_safetensors=True,
            ).to(self.device, dtype=get_torch_dtype(self.sd_ref().dtype))
        elif self.config.image_encoder_arch == 'vit-hybrid':
            try:
                self.clip_image_processor = ViTHybridImageProcessor.from_pretrained(adapter_config.image_encoder_path)
            except EnvironmentError:
                print(f"could not load image processor from {adapter_config.image_encoder_path}")
                self.clip_image_processor = ViTHybridImageProcessor(
                    size=320,
                    image_mean=[0.48145466, 0.4578275, 0.40821073],
                    image_std=[0.26862954, 0.26130258, 0.27577711],
                )
            self.image_encoder = ViTHybridForImageClassification.from_pretrained(
                adapter_config.image_encoder_path,
                use_safetensors=True,
            ).to(self.device, dtype=get_torch_dtype(self.sd_ref().dtype))
        else:
            raise ValueError(f"unknown image encoder arch: {adapter_config.image_encoder_arch}")

        self.input_size = self.image_encoder.config.image_size

        if self.config.image_encoder_arch == 'clip+':
            # self.clip_image_processor.config
            # We do a 3x downscale of the image, so we need to adjust the input size
            preprocessor_input_size = self.image_encoder.config.image_size * 4

            # update the preprocessor so images come in at the right size
            self.clip_image_processor.size['shortest_edge'] = preprocessor_input_size
            self.clip_image_processor.crop_size['height'] = preprocessor_input_size
            self.clip_image_processor.crop_size['width'] = preprocessor_input_size

            self.preprocessor = CLIPImagePreProcessor(
                input_size=preprocessor_input_size,
                clip_input_size=self.image_encoder.config.image_size,
            )
        if 'height' in self.clip_image_processor.size:
            self.input_size = self.clip_image_processor.size['height']
        else:
            self.input_size = self.clip_image_processor.crop_size['height']
        self.current_scale = 1.0
        self.is_active = True
        if adapter_config.type == 'ip':
            # ip-adapter
            image_proj_model = ImageProjModel(
                cross_attention_dim=sd.unet.config['cross_attention_dim'],
                clip_embeddings_dim=self.image_encoder.config.projection_dim,
                clip_extra_context_tokens=self.config.num_tokens,  # usually 4
            )
        elif adapter_config.type == 'ip+':
            heads = 12 if not sd.is_xl else 20
            dim = sd.unet.config['cross_attention_dim'] if not sd.is_xl else 1280
            embedding_dim = self.image_encoder.config.hidden_size if not self.config.image_encoder_arch == "convnext" else \
            self.image_encoder.config.hidden_sizes[-1]

            image_encoder_state_dict = self.image_encoder.state_dict()
            # max_seq_len = CLIP tokens + CLS token
            max_seq_len = 257
            if "vision_model.embeddings.position_embedding.weight" in image_encoder_state_dict:
                # clip
                max_seq_len = int(image_encoder_state_dict["vision_model.embeddings.position_embedding.weight"].shape[0])

            # ip-adapter-plus
            image_proj_model = Resampler(
                dim=dim,
                depth=4,
                dim_head=64,
                heads=heads,
                num_queries=self.config.num_tokens if self.config.num_tokens > 0 else max_seq_len,
                embedding_dim=embedding_dim,
                max_seq_len=max_seq_len,
                output_dim=sd.unet.config['cross_attention_dim'],
                ff_mult=4
            )
        elif adapter_config.type == 'ilora':
            # we apply the clip encodings to the LoRA
            image_proj_model = None
        else:
            raise ValueError(f"unknown adapter type: {adapter_config.type}")

        # init adapter modules
        attn_procs = {}
        unet_sd = sd.unet.state_dict()
        for name in sd.unet.attn_processors.keys():
            cross_attention_dim = None if name.endswith("attn1.processor") else sd.unet.config['cross_attention_dim']
            if name.startswith("mid_block"):
                hidden_size = sd.unet.config['block_out_channels'][-1]
            elif name.startswith("up_blocks"):
                block_id = int(name[len("up_blocks.")])
                hidden_size = list(reversed(sd.unet.config['block_out_channels']))[block_id]
            elif name.startswith("down_blocks"):
                block_id = int(name[len("down_blocks.")])
                hidden_size = sd.unet.config['block_out_channels'][block_id]
            else:
                # they didnt have this, but would lead to undefined below
                raise ValueError(f"unknown attn processor name: {name}")
            if cross_attention_dim is None:
                attn_procs[name] = AttnProcessor2_0()
            else:
                layer_name = name.split(".processor")[0]
                weights = {
                    "to_k_ip.weight": unet_sd[layer_name + ".to_k.weight"],
                    "to_v_ip.weight": unet_sd[layer_name + ".to_v.weight"],
                }

                attn_procs[name] = CustomIPAttentionProcessor(
                    hidden_size=hidden_size,
                    cross_attention_dim=cross_attention_dim,
                    scale=1.0,
                    num_tokens=self.config.num_tokens,
                    adapter=self
                )
                attn_procs[name].load_state_dict(weights)
        sd.unet.set_attn_processor(attn_procs)
        adapter_modules = torch.nn.ModuleList(sd.unet.attn_processors.values())

        sd.adapter = self
        self.unet_ref: weakref.ref = weakref.ref(sd.unet)
        self.image_proj_model = image_proj_model
        self.adapter_modules = adapter_modules
        # load the weights if we have some
        if self.config.name_or_path:
            loaded_state_dict = load_ip_adapter_model(
                self.config.name_or_path,
                device='cpu',
                dtype=sd.torch_dtype
            )
            self.load_state_dict(loaded_state_dict)

        self.set_scale(1.0)

        if self.config.train_image_encoder:
            self.image_encoder.train()
            self.image_encoder.requires_grad_(True)

    def to(self, *args, **kwargs):
        super().to(*args, **kwargs)
        self.image_encoder.to(*args, **kwargs)
        self.image_proj_model.to(*args, **kwargs)
        self.adapter_modules.to(*args, **kwargs)
        if self.preprocessor is not None:
            self.preprocessor.to(*args, **kwargs)
        return self

    def load_ip_adapter(self, state_dict: Union[OrderedDict, dict]):
        self.image_proj_model.load_state_dict(state_dict["image_proj"])
        ip_layers = torch.nn.ModuleList(self.pipe.unet.attn_processors.values())
        ip_layers.load_state_dict(state_dict["ip_adapter"])
        if self.config.train_image_encoder and 'image_encoder' in state_dict:
            self.image_encoder.load_state_dict(state_dict["image_encoder"])
        if self.preprocessor is not None and 'preprocessor' in state_dict:
            self.preprocessor.load_state_dict(state_dict["preprocessor"])

    # def load_state_dict(self, state_dict: Union[OrderedDict, dict]):
    #     self.load_ip_adapter(state_dict)

    def state_dict(self) -> OrderedDict:
        state_dict = OrderedDict()
        state_dict["image_proj"] = self.image_proj_model.state_dict()
        state_dict["ip_adapter"] = self.adapter_modules.state_dict()
        if self.config.train_image_encoder:
            state_dict["image_encoder"] = self.image_encoder.state_dict()
        if self.preprocessor is not None:
            state_dict["preprocessor"] = self.preprocessor.state_dict()
        return state_dict

    def get_scale(self):
        return self.current_scale

    def set_scale(self, scale):
        self.current_scale = scale
        for attn_processor in self.sd_ref().unet.attn_processors.values():
            if isinstance(attn_processor, CustomIPAttentionProcessor):
                attn_processor.scale = scale

    # @torch.no_grad()
    # def get_clip_image_embeds_from_pil(self, pil_image: Union[Image.Image, List[Image.Image]],
    #                                    drop=False) -> torch.Tensor:
    #     # todo: add support for sdxl
    #     if isinstance(pil_image, Image.Image):
    #         pil_image = [pil_image]
    #     clip_image = self.clip_image_processor(images=pil_image, return_tensors="pt").pixel_values
    #     clip_image = clip_image.to(self.device, dtype=get_torch_dtype(self.sd_ref().dtype))
    #     if drop:
    #         clip_image = clip_image * 0
    #     clip_image_embeds = self.image_encoder(clip_image, output_hidden_states=True).hidden_states[-2]
    #     return clip_image_embeds

    def get_clip_image_embeds_from_tensors(
            self,
            tensors_0_1: torch.Tensor,
            drop=False,
            is_training=False,
            has_been_preprocessed=False
    ) -> torch.Tensor:
        with torch.no_grad():
            # on training the clip image is created in the dataloader
            if not has_been_preprocessed:
                # tensors should be 0-1
                if tensors_0_1.ndim == 3:
                    tensors_0_1 = tensors_0_1.unsqueeze(0)
                # training tensors are 0 - 1
                tensors_0_1 = tensors_0_1.to(self.device, dtype=torch.float16)
                # if images are out of this range throw error
                if tensors_0_1.min() < -0.3 or tensors_0_1.max() > 1.3:
                    raise ValueError("image tensor values must be between 0 and 1. Got min: {}, max: {}".format(
                        tensors_0_1.min(), tensors_0_1.max()
                    ))
                clip_image = self.clip_image_processor(
                    images=tensors_0_1,
                    return_tensors="pt",
                    do_resize=True,
                    do_rescale=False,
                ).pixel_values
            else:
                clip_image = tensors_0_1
            clip_image = clip_image.to(self.device, dtype=get_torch_dtype(self.sd_ref().dtype)).detach()
            if drop:
                clip_image = clip_image * 0
        with torch.set_grad_enabled(is_training):
            if is_training:
                self.image_encoder.train()
                clip_image = clip_image.requires_grad_(True)
                if self.preprocessor is not None:
                    clip_image = self.preprocessor(clip_image)
                clip_output = self.image_encoder(
                    clip_image,
                    output_hidden_states=True
                )
            else:
                self.image_encoder.eval()
                if self.preprocessor is not None:
                    clip_image = self.preprocessor(clip_image)
                clip_output = self.image_encoder(
                    clip_image, output_hidden_states=True
                )

            if self.config.type.startswith('ip+'):
                # they skip last layer for ip+
                # https://github.com/tencent-ailab/IP-Adapter/blob/f4b6742db35ea6d81c7b829a55b0a312c7f5a677/tutorial_train_plus.py#L403C26-L403C26
                clip_image_embeds = clip_output.hidden_states[-2]
            else:
                clip_image_embeds = clip_output.image_embeds
        return clip_image_embeds

    # use drop for prompt dropout, or negatives
    def forward(self, embeddings: PromptEmbeds, clip_image_embeds: torch.Tensor) -> PromptEmbeds:
        clip_image_embeds = clip_image_embeds.to(self.device, dtype=get_torch_dtype(self.sd_ref().dtype))
        image_prompt_embeds = self.image_proj_model(clip_image_embeds)
        embeddings.text_embeds = torch.cat([embeddings.text_embeds, image_prompt_embeds], dim=1)
        return embeddings

    def parameters(self, recurse: bool = True) -> Iterator[Parameter]:
        for attn_processor in self.adapter_modules:
            yield from attn_processor.parameters(recurse)
        yield from self.image_proj_model.parameters(recurse)
        if self.config.train_image_encoder:
            yield from self.image_encoder.parameters(recurse)
        if self.preprocessor is not None:
            yield from self.preprocessor.parameters(recurse)

    def merge_in_weights(self, state_dict: Mapping[str, Any]):
        # merge in img_proj weights
        current_img_proj_state_dict = self.image_proj_model.state_dict()
        for key, value in state_dict["image_proj"].items():
            if key in current_img_proj_state_dict:
                current_shape = current_img_proj_state_dict[key].shape
                new_shape = value.shape
                if current_shape != new_shape:
                    try:
                        # merge in what we can and leave the other values as they are
                        if len(current_shape) == 1:
                            current_img_proj_state_dict[key][:new_shape[0]] = value
                        elif len(current_shape) == 2:
                            current_img_proj_state_dict[key][:new_shape[0], :new_shape[1]] = value
                        elif len(current_shape) == 3:
                            current_img_proj_state_dict[key][:new_shape[0], :new_shape[1], :new_shape[2]] = value
                        elif len(current_shape) == 4:
                            current_img_proj_state_dict[key][:new_shape[0], :new_shape[1], :new_shape[2],
                            :new_shape[3]] = value
                        else:
                            raise ValueError(f"unknown shape: {current_shape}")
                    except RuntimeError as e:
                        print(e)
                        print(f"could not merge in {key}: {list(current_shape)} <<< {list(new_shape)}. Trying other way")

                        if len(current_shape) == 1:
                            current_img_proj_state_dict[key][:current_shape[0]] = value[:current_shape[0]]
                        elif len(current_shape) == 2:
                            current_img_proj_state_dict[key][:current_shape[0], :current_shape[1]] = value[:current_shape[0], :current_shape[1]]
                        elif len(current_shape) == 3:
                            current_img_proj_state_dict[key][:current_shape[0], :current_shape[1], :current_shape[2]] = value[:current_shape[0], :current_shape[1], :current_shape[2]]
                        elif len(current_shape) == 4:
                            current_img_proj_state_dict[key][:current_shape[0], :current_shape[1], :current_shape[2],
                            :current_shape[3]] = value[:current_shape[0], :current_shape[1], :current_shape[2],
                            :current_shape[3]]
                        else:
                            raise ValueError(f"unknown shape: {current_shape}")
                        print(f"Force merged in {key}: {list(current_shape)} <<< {list(new_shape)}")
                else:
                    current_img_proj_state_dict[key] = value
        self.image_proj_model.load_state_dict(current_img_proj_state_dict)

        # merge in ip adapter weights
        current_ip_adapter_state_dict = self.adapter_modules.state_dict()
        for key, value in state_dict["ip_adapter"].items():
            if key in current_ip_adapter_state_dict:
                current_shape = current_ip_adapter_state_dict[key].shape
                new_shape = value.shape
                if current_shape != new_shape:
                    try:
                        # merge in what we can and leave the other values as they are
                        if len(current_shape) == 1:
                            current_ip_adapter_state_dict[key][:new_shape[0]] = value
                        elif len(current_shape) == 2:
                            current_ip_adapter_state_dict[key][:new_shape[0], :new_shape[1]] = value
                        elif len(current_shape) == 3:
                            current_ip_adapter_state_dict[key][:new_shape[0], :new_shape[1], :new_shape[2]] = value
                        elif len(current_shape) == 4:
                            current_ip_adapter_state_dict[key][:new_shape[0], :new_shape[1], :new_shape[2],
                            :new_shape[3]] = value
                        else:
                            raise ValueError(f"unknown shape: {current_shape}")
                        print(f"Force merged in {key}: {list(current_shape)} <<< {list(new_shape)}")
                    except RuntimeError as e:
                        print(e)
                        print(f"could not merge in {key}: {list(current_shape)} <<< {list(new_shape)}. Trying other way")

                        if(len(current_shape) == 1):
                            current_ip_adapter_state_dict[key][:current_shape[0]] = value[:current_shape[0]]
                        elif(len(current_shape) == 2):
                            current_ip_adapter_state_dict[key][:current_shape[0], :current_shape[1]] = value[:current_shape[0], :current_shape[1]]
                        elif(len(current_shape) == 3):
                            current_ip_adapter_state_dict[key][:current_shape[0], :current_shape[1], :current_shape[2]] = value[:current_shape[0], :current_shape[1], :current_shape[2]]
                        elif(len(current_shape) == 4):
                            current_ip_adapter_state_dict[key][:current_shape[0], :current_shape[1], :current_shape[2], :current_shape[3]] = value[:current_shape[0], :current_shape[1], :current_shape[2], :current_shape[3]]
                        else:
                            raise ValueError(f"unknown shape: {current_shape}")
                        print(f"Force merged in {key}: {list(current_shape)} <<< {list(new_shape)}")

                else:
                    current_ip_adapter_state_dict[key] = value
        self.adapter_modules.load_state_dict(current_ip_adapter_state_dict)


    def load_state_dict(self, state_dict: Mapping[str, Any], strict: bool = True):
        strict = False
        try:
            self.image_proj_model.load_state_dict(state_dict["image_proj"], strict=strict)
            self.adapter_modules.load_state_dict(state_dict["ip_adapter"], strict=strict)
        except Exception as e:
            print(e)
            print("could not load ip adapter weights, trying to merge in weights")
            self.merge_in_weights(state_dict)
        if self.config.train_image_encoder and 'image_encoder' in state_dict:
            self.image_encoder.load_state_dict(state_dict["image_encoder"], strict=strict)
        if self.preprocessor is not None and 'preprocessor' in state_dict:
            self.preprocessor.load_state_dict(state_dict["preprocessor"], strict=strict)

    def enable_gradient_checkpointing(self):
        self.image_encoder.gradient_checkpointing = True
