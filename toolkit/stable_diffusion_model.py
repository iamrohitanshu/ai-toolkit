import copy
import gc
import json
import random
import shutil
import typing
from typing import Union, List, Literal, Iterator
import sys
import os
from collections import OrderedDict

import yaml
from PIL import Image
from diffusers.pipelines.stable_diffusion_xl.pipeline_stable_diffusion_xl import rescale_noise_cfg
from safetensors.torch import save_file, load_file
from torch.nn import Parameter
from torch.utils.checkpoint import checkpoint
from tqdm import tqdm
from torchvision.transforms import Resize, transforms

from toolkit.clip_vision_adapter import ClipVisionAdapter
from toolkit.custom_adapter import CustomAdapter
from toolkit.ip_adapter import IPAdapter
from library.model_util import convert_unet_state_dict_to_sd, convert_text_encoder_state_dict_to_sd_v2, \
    convert_vae_state_dict, load_vae
from toolkit import train_tools
from toolkit.config_modules import ModelConfig, GenerateImageConfig
from toolkit.metadata import get_meta_for_safetensors
from toolkit.paths import REPOS_ROOT, KEYMAPS_ROOT
from toolkit.prompt_utils import inject_trigger_into_prompt, PromptEmbeds, concat_prompt_embeds
from toolkit.reference_adapter import ReferenceAdapter
from toolkit.sampler import get_sampler
from toolkit.saving import save_ldm_model_from_diffusers, get_ldm_state_dict_from_diffusers
from toolkit.sd_device_states_presets import empty_preset
from toolkit.train_tools import get_torch_dtype, apply_noise_offset
import torch
from toolkit.pipelines import CustomStableDiffusionXLPipeline, CustomStableDiffusionPipeline, \
    StableDiffusionKDiffusionXLPipeline, StableDiffusionXLRefinerPipeline
from diffusers import StableDiffusionPipeline, StableDiffusionXLPipeline, T2IAdapter, DDPMScheduler, \
    StableDiffusionXLAdapterPipeline, StableDiffusionAdapterPipeline, DiffusionPipeline, \
    StableDiffusionXLImg2ImgPipeline, LCMScheduler
import diffusers
from diffusers import \
    AutoencoderKL, \
    UNet2DConditionModel

from toolkit.paths import ORIG_CONFIGS_ROOT, DIFFUSERS_CONFIGS_ROOT

# tell it to shut up
diffusers.logging.set_verbosity(diffusers.logging.ERROR)

SD_PREFIX_VAE = "vae"
SD_PREFIX_UNET = "unet"
SD_PREFIX_REFINER_UNET = "refiner_unet"
SD_PREFIX_TEXT_ENCODER = "te"

SD_PREFIX_TEXT_ENCODER1 = "te0"
SD_PREFIX_TEXT_ENCODER2 = "te1"

# prefixed diffusers keys
DO_NOT_TRAIN_WEIGHTS = [
    "unet_time_embedding.linear_1.bias",
    "unet_time_embedding.linear_1.weight",
    "unet_time_embedding.linear_2.bias",
    "unet_time_embedding.linear_2.weight",
    "refiner_unet_time_embedding.linear_1.bias",
    "refiner_unet_time_embedding.linear_1.weight",
    "refiner_unet_time_embedding.linear_2.bias",
    "refiner_unet_time_embedding.linear_2.weight",
]

DeviceStatePreset = Literal['cache_latents', 'generate']


class BlankNetwork:

    def __init__(self):
        self.multiplier = 1.0
        self.is_active = True
        self.is_merged_in = False
        self.can_merge_in = False

    def __enter__(self):
        self.is_active = True

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.is_active = False


def flush():
    torch.cuda.empty_cache()
    gc.collect()


UNET_IN_CHANNELS = 4  # Stable Diffusion の in_channels は 4 で固定。XLも同じ。
# VAE_SCALE_FACTOR = 8  # 2 ** (len(vae.config.block_out_channels) - 1) = 8

# if is type checking
if typing.TYPE_CHECKING:
    from diffusers.schedulers import KarrasDiffusionSchedulers
    from transformers import CLIPTextModel, CLIPTokenizer, CLIPTextModelWithProjection


class StableDiffusion:

    def __init__(
            self,
            device,
            model_config: ModelConfig,
            dtype='fp16',
            custom_pipeline=None,
            noise_scheduler=None,
    ):
        self.custom_pipeline = custom_pipeline
        self.device = device
        self.dtype = dtype
        self.torch_dtype = get_torch_dtype(dtype)
        self.device_torch = torch.device(self.device)
        self.model_config = model_config
        self.prediction_type = "v_prediction" if self.model_config.is_v_pred else "epsilon"

        self.device_state = None

        self.pipeline: Union[None, 'StableDiffusionPipeline', 'CustomStableDiffusionXLPipeline']
        self.vae: Union[None, 'AutoencoderKL']
        self.unet: Union[None, 'UNet2DConditionModel']
        self.text_encoder: Union[None, 'CLIPTextModel', List[Union['CLIPTextModel', 'CLIPTextModelWithProjection']]]
        self.tokenizer: Union[None, 'CLIPTokenizer', List['CLIPTokenizer']]
        self.noise_scheduler: Union[None, 'DDPMScheduler'] = noise_scheduler

        self.refiner_unet: Union[None, 'UNet2DConditionModel'] = None

        # sdxl stuff
        self.logit_scale = None
        self.ckppt_info = None
        self.is_loaded = False

        # to hold network if there is one
        self.network = None
        self.adapter: Union['T2IAdapter', 'IPAdapter', 'ReferenceAdapter', None] = None
        self.is_xl = model_config.is_xl
        self.is_v2 = model_config.is_v2
        self.is_ssd = model_config.is_ssd
        self.is_vega = model_config.is_vega

        self.use_text_encoder_1 = model_config.use_text_encoder_1
        self.use_text_encoder_2 = model_config.use_text_encoder_2

        self.config_file = None

    def load_model(self):
        if self.is_loaded:
            return
        dtype = get_torch_dtype(self.dtype)
        # sch = KDPM2DiscreteScheduler
        if self.noise_scheduler is None:
            scheduler = get_sampler(
                'ddpm', {
                "prediction_type": self.prediction_type,
            })
            self.noise_scheduler = scheduler

        # move the betas alphas and  alphas_cumprod to device. Sometimed they get stuck on cpu, not sure why
        # self.noise_scheduler.betas = self.noise_scheduler.betas.to(self.device_torch)
        # self.noise_scheduler.alphas = self.noise_scheduler.alphas.to(self.device_torch)
        # self.noise_scheduler.alphas_cumprod = self.noise_scheduler.alphas_cumprod.to(self.device_torch)

        model_path = self.model_config.name_or_path
        if 'civitai.com' in self.model_config.name_or_path:
            # load is a civit ai model, use the loader.
            from toolkit.civitai import get_model_path_from_url
            model_path = get_model_path_from_url(self.model_config.name_or_path)

        load_args = {
            'scheduler': self.noise_scheduler,
        }
        if self.model_config.vae_path is not None:
            load_args['vae'] = load_vae(self.model_config.vae_path, dtype)

        if self.model_config.is_xl or self.model_config.is_ssd or self.model_config.is_vega:
            if self.custom_pipeline is not None:
                pipln = self.custom_pipeline
            else:
                pipln = StableDiffusionXLPipeline
                # pipln = StableDiffusionKDiffusionXLPipeline

            # see if path exists
            if not os.path.exists(model_path) or os.path.isdir(model_path):
                # try to load with default diffusers
                pipe = pipln.from_pretrained(
                    model_path,
                    dtype=dtype,
                    device=self.device_torch,
                    variant="fp16",
                    use_safetensors=True,
                    **load_args
                )
            else:
                pipe = pipln.from_single_file(
                    model_path,
                    device=self.device_torch,
                    torch_dtype=self.torch_dtype,
                )

            if 'vae' in load_args and load_args['vae'] is not None:
                pipe.vae = load_args['vae']
            flush()

            text_encoders = [pipe.text_encoder, pipe.text_encoder_2]
            tokenizer = [pipe.tokenizer, pipe.tokenizer_2]
            for text_encoder in text_encoders:
                text_encoder.to(self.device_torch, dtype=dtype)
                text_encoder.requires_grad_(False)
                text_encoder.eval()
            text_encoder = text_encoders

            if self.model_config.experimental_xl:
                print("Experimental XL mode enabled")
                print("Loading and injecting alt weights")
                # load the mismatched weight and force it in
                raw_state_dict = load_file(model_path)
                replacement_weight = raw_state_dict['conditioner.embedders.1.model.text_projection'].clone()
                del raw_state_dict
                #  get state dict for  for 2nd text encoder
                te1_state_dict = text_encoders[1].state_dict()
                # replace weight with mismatched weight
                te1_state_dict['text_projection.weight'] = replacement_weight.to(self.device_torch, dtype=dtype)
                flush()
                print("Injecting alt weights")

        else:
            if self.custom_pipeline is not None:
                pipln = self.custom_pipeline
            else:
                pipln = StableDiffusionPipeline

            # see if path exists
            if not os.path.exists(model_path) or os.path.isdir(model_path):
                # try to load with default diffusers
                pipe = pipln.from_pretrained(
                    model_path,
                    dtype=dtype,
                    device=self.device_torch,
                    load_safety_checker=False,
                    requires_safety_checker=False,
                    safety_checker=None,
                    variant="fp16",
                    **load_args
                ).to(self.device_torch)
            else:
                pipe = pipln.from_single_file(
                    model_path,
                    dtype=dtype,
                    device=self.device_torch,
                    load_safety_checker=False,
                    requires_safety_checker=False,
                    torch_dtype=self.torch_dtype,
                    safety_checker=False,
                    **load_args
                ).to(self.device_torch)
            flush()

            pipe.register_to_config(requires_safety_checker=False)
            text_encoder = pipe.text_encoder
            text_encoder.to(self.device_torch, dtype=dtype)
            text_encoder.requires_grad_(False)
            text_encoder.eval()
            tokenizer = pipe.tokenizer

        # scheduler doesn't get set sometimes, so we set it here
        pipe.scheduler = self.noise_scheduler

        # add hacks to unet to help training
        # pipe.unet = prepare_unet_for_training(pipe.unet)

        self.unet: 'UNet2DConditionModel' = pipe.unet
        self.vae: 'AutoencoderKL' = pipe.vae.to(self.device_torch, dtype=dtype)
        self.vae.eval()
        self.vae.requires_grad_(False)
        self.unet.to(self.device_torch, dtype=dtype)
        self.unet.requires_grad_(False)
        self.unet.eval()

        self.tokenizer = tokenizer
        self.text_encoder = text_encoder
        self.pipeline = pipe
        self.load_refiner()
        self.is_loaded = True

    def te_train(self):
        if isinstance(self.text_encoder, list):
            for te in self.text_encoder:
                te.train()
        else:
            self.text_encoder.train()

    def te_eval(self):
        if isinstance(self.text_encoder, list):
            for te in self.text_encoder:
                te.eval()
        else:
            self.text_encoder.eval()

    def load_refiner(self):
        # for now, we are just going to rely on the TE from the base model
        # which is TE2 for SDXL and TE for SD (no refiner currently)
        # and completely ignore a TE that may or may not be packaged with the refiner
        if self.model_config.refiner_name_or_path is not None:
            refiner_config_path = os.path.join(ORIG_CONFIGS_ROOT, 'sd_xl_refiner.yaml')
            # load the refiner model
            dtype = get_torch_dtype(self.dtype)
            model_path = self.model_config.refiner_name_or_path
            if not os.path.exists(model_path) or os.path.isdir(model_path):
                # TODO only load unet??
                refiner = StableDiffusionXLImg2ImgPipeline.from_pretrained(
                    model_path,
                    dtype=dtype,
                    device=self.device_torch,
                    variant="fp16",
                    use_safetensors=True,
                ).to(self.device_torch)
            else:
                refiner = StableDiffusionXLImg2ImgPipeline.from_single_file(
                    model_path,
                    dtype=dtype,
                    device=self.device_torch,
                    torch_dtype=self.torch_dtype,
                    original_config_file=refiner_config_path,
                ).to(self.device_torch)

            self.refiner_unet = refiner.unet
            del refiner
            flush()

    @torch.no_grad()
    def generate_images(
            self,
            image_configs: List[GenerateImageConfig],
            sampler=None,
            pipeline: Union[None, StableDiffusionPipeline, StableDiffusionXLPipeline] = None,
    ):
        merge_multiplier = 1.0
        # sample_folder = os.path.join(self.save_root, 'samples')
        if self.network is not None:
            self.network.eval()
            network = self.network
            # check if we have the same network weight for all samples. If we do, we can merge in th
            # the network to drastically speed up inference
            unique_network_weights = set([x.network_multiplier for x in image_configs])
            if len(unique_network_weights) == 1 and self.network.can_merge_in:
                can_merge_in = True
                merge_multiplier = unique_network_weights.pop()
                network.merge_in(merge_weight=merge_multiplier)
        else:
            network = BlankNetwork()

        self.save_device_state()
        self.set_device_state_preset('generate')

        # save current seed state for training
        rng_state = torch.get_rng_state()
        cuda_rng_state = torch.cuda.get_rng_state() if torch.cuda.is_available() else None

        if pipeline is None:
            noise_scheduler = self.noise_scheduler
            if sampler is not None:
                if sampler.startswith("sample_"):  # sample_dpmpp_2m
                    # using ksampler
                    noise_scheduler = get_sampler(
                        'lms', {
                            "prediction_type": self.prediction_type,
                        })
                else:
                    noise_scheduler = get_sampler(
                        sampler,
                        {
                            "prediction_type": self.prediction_type,
                        }
                    )

                try:
                    noise_scheduler = noise_scheduler.to(self.device_torch, self.torch_dtype)
                except:
                    pass

            if sampler.startswith("sample_") and self.is_xl:
                # using kdiffusion
                Pipe = StableDiffusionKDiffusionXLPipeline
            elif self.is_xl:
                Pipe = StableDiffusionXLPipeline
            else:
                Pipe = StableDiffusionPipeline

            extra_args = {}
            if self.adapter is not None:
                if isinstance(self.adapter, T2IAdapter):
                    if self.is_xl:
                        Pipe = StableDiffusionXLAdapterPipeline
                    else:
                        Pipe = StableDiffusionAdapterPipeline
                    extra_args['adapter'] = self.adapter
                elif isinstance(self.adapter, ReferenceAdapter):
                    # pass the noise scheduler to the adapter
                    self.adapter.noise_scheduler = noise_scheduler
                else:
                    if self.is_xl:
                        extra_args['add_watermarker'] = False

            # TODO add clip skip
            if self.is_xl:
                pipeline = Pipe(
                    vae=self.vae,
                    unet=self.unet,
                    text_encoder=self.text_encoder[0],
                    text_encoder_2=self.text_encoder[1],
                    tokenizer=self.tokenizer[0],
                    tokenizer_2=self.tokenizer[1],
                    scheduler=noise_scheduler,
                    **extra_args
                ).to(self.device_torch)
                pipeline.watermark = None
            else:
                pipeline = Pipe(
                    vae=self.vae,
                    unet=self.unet,
                    text_encoder=self.text_encoder,
                    tokenizer=self.tokenizer,
                    scheduler=noise_scheduler,
                    safety_checker=None,
                    feature_extractor=None,
                    requires_safety_checker=False,
                    **extra_args
                ).to(self.device_torch)
            flush()
            # disable progress bar
            pipeline.set_progress_bar_config(disable=True)

            if sampler.startswith("sample_"):
                pipeline.set_scheduler(sampler)

        refiner_pipeline = None
        if self.refiner_unet:
            # build refiner pipeline
            refiner_pipeline = StableDiffusionXLImg2ImgPipeline(
                vae=pipeline.vae,
                unet=self.refiner_unet,
                text_encoder=None,
                text_encoder_2=pipeline.text_encoder_2,
                tokenizer=None,
                tokenizer_2=pipeline.tokenizer_2,
                scheduler=pipeline.scheduler,
                add_watermarker=False,
                requires_aesthetics_score=True,
            ).to(self.device_torch)
            # refiner_pipeline.register_to_config(requires_aesthetics_score=False)
            refiner_pipeline.watermark = None
            refiner_pipeline.set_progress_bar_config(disable=True)
            flush()

        start_multiplier = 1.0
        if self.network is not None:
            start_multiplier = self.network.multiplier

        pipeline.to(self.device_torch)

        with network:
            with torch.no_grad():
                if self.network is not None:
                    assert self.network.is_active

                for i in tqdm(range(len(image_configs)), desc=f"Generating Images", leave=False):
                    gen_config = image_configs[i]

                    extra = {}
                    if self.adapter is not None and gen_config.adapter_image_path is not None:
                        validation_image = Image.open(gen_config.adapter_image_path).convert("RGB")
                        if isinstance(self.adapter, T2IAdapter):
                            # not sure why this is double??
                            validation_image = validation_image.resize((gen_config.width * 2, gen_config.height * 2))
                            extra['image'] = validation_image
                            extra['adapter_conditioning_scale'] = gen_config.adapter_conditioning_scale
                        if isinstance(self.adapter, IPAdapter) or isinstance(self.adapter, ClipVisionAdapter):
                            transform = transforms.Compose([
                                transforms.ToTensor(),
                            ])
                            validation_image = transform(validation_image)
                        if isinstance(self.adapter, CustomAdapter):
                            # todo allow loading multiple
                            transform = transforms.Compose([
                                transforms.ToTensor(),
                            ])
                            validation_image = transform(validation_image)
                            self.adapter.num_images = 1
                        if isinstance(self.adapter, ReferenceAdapter):
                            # need -1 to 1
                            validation_image = transforms.ToTensor()(validation_image)
                            validation_image = validation_image * 2.0 - 1.0
                            validation_image = validation_image.unsqueeze(0)
                            self.adapter.set_reference_images(validation_image)

                    if self.network is not None:
                        self.network.multiplier = gen_config.network_multiplier
                    torch.manual_seed(gen_config.seed)
                    torch.cuda.manual_seed(gen_config.seed)

                    if self.adapter is not None and isinstance(self.adapter, ClipVisionAdapter) \
                            and gen_config.adapter_image_path is not None:
                        # run through the adapter to saturate the embeds
                        conditional_clip_embeds = self.adapter.get_clip_image_embeds_from_tensors(validation_image)
                        self.adapter(conditional_clip_embeds)

                    if self.adapter is not None and isinstance(self.adapter, CustomAdapter):
                        # handle condition the prompts
                        gen_config.prompt = self.adapter.condition_prompt(
                            gen_config.prompt,
                            is_unconditional=False,
                        )
                        gen_config.prompt_2 = gen_config.prompt
                        gen_config.negative_prompt = self.adapter.condition_prompt(
                            gen_config.negative_prompt,
                            is_unconditional=True,
                        )
                        gen_config.negative_prompt_2 = gen_config.negative_prompt

                    # encode the prompt ourselves so we can do fun stuff with embeddings
                    conditional_embeds = self.encode_prompt(gen_config.prompt, gen_config.prompt_2, force_all=True)

                    unconditional_embeds = self.encode_prompt(
                        gen_config.negative_prompt, gen_config.negative_prompt_2, force_all=True
                    )

                    # allow any manipulations to take place to embeddings
                    gen_config.post_process_embeddings(
                        conditional_embeds,
                        unconditional_embeds,
                    )

                    if self.adapter is not None and isinstance(self.adapter, IPAdapter) \
                            and gen_config.adapter_image_path is not None:

                        # apply the image projection
                        conditional_clip_embeds = self.adapter.get_clip_image_embeds_from_tensors(validation_image)
                        unconditional_clip_embeds = self.adapter.get_clip_image_embeds_from_tensors(validation_image,
                                                                                                    True)
                        conditional_embeds = self.adapter(conditional_embeds, conditional_clip_embeds)
                        unconditional_embeds = self.adapter(unconditional_embeds, unconditional_clip_embeds)

                    if self.adapter is not None and isinstance(self.adapter, CustomAdapter):
                        conditional_embeds = self.adapter.condition_encoded_embeds(
                            tensors_0_1=validation_image,
                            prompt_embeds=conditional_embeds,
                            is_training=False,
                            has_been_preprocessed=False,
                        )
                        unconditional_embeds = self.adapter.condition_encoded_embeds(
                            tensors_0_1=validation_image,
                            prompt_embeds=unconditional_embeds,
                            is_training=False,
                            has_been_preprocessed=False,
                            is_unconditional=True,
                        )

                    if self.refiner_unet is not None and gen_config.refiner_start_at < 1.0:
                        # if we have a refiner loaded, set the denoising end at the refiner start
                        extra['denoising_end'] = gen_config.refiner_start_at
                        extra['output_type'] = 'latent'
                        if not self.is_xl:
                            raise ValueError("Refiner is only supported for XL models")

                    if self.is_xl:
                        # fix guidance rescale for sdxl
                        # was trained on 0.7 (I believe)

                        grs = gen_config.guidance_rescale
                        # if grs is None or grs < 0.00001:
                        #     grs = 0.7
                        # grs = 0.0

                        if sampler.startswith("sample_"):
                            extra['use_karras_sigmas'] = True
                            extra = {
                                **extra,
                                **gen_config.extra_kwargs,
                            }

                        img = pipeline(
                            # prompt=gen_config.prompt,
                            # prompt_2=gen_config.prompt_2,
                            prompt_embeds=conditional_embeds.text_embeds,
                            pooled_prompt_embeds=conditional_embeds.pooled_embeds,
                            negative_prompt_embeds=unconditional_embeds.text_embeds,
                            negative_pooled_prompt_embeds=unconditional_embeds.pooled_embeds,
                            # negative_prompt=gen_config.negative_prompt,
                            # negative_prompt_2=gen_config.negative_prompt_2,
                            height=gen_config.height,
                            width=gen_config.width,
                            num_inference_steps=gen_config.num_inference_steps,
                            guidance_scale=gen_config.guidance_scale,
                            guidance_rescale=grs,
                            latents=gen_config.latents,
                            **extra
                        ).images[0]
                    else:
                        img = pipeline(
                            # prompt=gen_config.prompt,
                            prompt_embeds=conditional_embeds.text_embeds,
                            negative_prompt_embeds=unconditional_embeds.text_embeds,
                            # negative_prompt=gen_config.negative_prompt,
                            height=gen_config.height,
                            width=gen_config.width,
                            num_inference_steps=gen_config.num_inference_steps,
                            guidance_scale=gen_config.guidance_scale,
                            latents=gen_config.latents,
                            **extra
                        ).images[0]

                    if self.refiner_unet is not None and gen_config.refiner_start_at < 1.0:
                        # slide off just the last 1280 on the last dim as refiner does not use first text encoder
                        # todo, should we just use the Text encoder for the refiner? Fine tuned versions will differ
                        refiner_text_embeds = conditional_embeds.text_embeds[:, :, -1280:]
                        refiner_unconditional_text_embeds = unconditional_embeds.text_embeds[:, :, -1280:]
                        # run through refiner
                        img = refiner_pipeline(
                            # prompt=gen_config.prompt,
                            # prompt_2=gen_config.prompt_2,

                            # slice these as it does not use both text encoders
                            # height=gen_config.height,
                            # width=gen_config.width,
                            prompt_embeds=refiner_text_embeds,
                            pooled_prompt_embeds=conditional_embeds.pooled_embeds,
                            negative_prompt_embeds=refiner_unconditional_text_embeds,
                            negative_pooled_prompt_embeds=unconditional_embeds.pooled_embeds,
                            num_inference_steps=gen_config.num_inference_steps,
                            guidance_scale=gen_config.guidance_scale,
                            guidance_rescale=grs,
                            denoising_start=gen_config.refiner_start_at,
                            denoising_end=gen_config.num_inference_steps,
                            image=img.unsqueeze(0)
                        ).images[0]

                    gen_config.save_image(img, i)

                if self.adapter is not None and isinstance(self.adapter, ReferenceAdapter):
                    self.adapter.clear_memory()

        # clear pipeline and cache to reduce vram usage
        del pipeline
        if refiner_pipeline is not None:
            del refiner_pipeline
        torch.cuda.empty_cache()

        # restore training state
        torch.set_rng_state(rng_state)
        if cuda_rng_state is not None:
            torch.cuda.set_rng_state(cuda_rng_state)

        self.restore_device_state()
        if self.network is not None:
            self.network.train()
            self.network.multiplier = start_multiplier

        if network.is_merged_in:
            network.merge_out(merge_multiplier)
        # self.tokenizer.to(original_device_dict['tokenizer'])

    def get_latent_noise(
            self,
            height=None,
            width=None,
            pixel_height=None,
            pixel_width=None,
            batch_size=1,
            noise_offset=0.0,
    ):
        VAE_SCALE_FACTOR = 2 ** (len(self.vae.config['block_out_channels']) - 1)
        if height is None and pixel_height is None:
            raise ValueError("height or pixel_height must be specified")
        if width is None and pixel_width is None:
            raise ValueError("width or pixel_width must be specified")
        if height is None:
            height = pixel_height // VAE_SCALE_FACTOR
        if width is None:
            width = pixel_width // VAE_SCALE_FACTOR

        noise = torch.randn(
            (
                batch_size,
                self.unet.config['in_channels'],
                height,
                width,
            ),
            device=self.unet.device,
        )
        noise = apply_noise_offset(noise, noise_offset)
        return noise

    def get_time_ids_from_latents(self, latents: torch.Tensor, requires_aesthetic_score=False):
        VAE_SCALE_FACTOR = 2 ** (len(self.vae.config['block_out_channels']) - 1)
        if self.is_xl:
            bs, ch, h, w = list(latents.shape)

            height = h * VAE_SCALE_FACTOR
            width = w * VAE_SCALE_FACTOR

            dtype = latents.dtype
            # just do it without any cropping nonsense
            target_size = (height, width)
            original_size = (height, width)
            crops_coords_top_left = (0, 0)
            if requires_aesthetic_score:
                # refiner
                # https://huggingface.co/papers/2307.01952
                aesthetic_score = 6.0  # simulate one
                add_time_ids = list(original_size + crops_coords_top_left + (aesthetic_score,))
            else:
                add_time_ids = list(original_size + crops_coords_top_left + target_size)
            add_time_ids = torch.tensor([add_time_ids])
            add_time_ids = add_time_ids.to(latents.device, dtype=dtype)

            batch_time_ids = torch.cat(
                [add_time_ids for _ in range(bs)]
            )
            return batch_time_ids
        else:
            return None

    def add_noise(
            self,
            original_samples: torch.FloatTensor,
            noise: torch.FloatTensor,
            timesteps: torch.IntTensor
    ) -> torch.FloatTensor:
        # we handle adding noise for the various schedulers here. Some
        # schedulers reference timesteps while others reference idx
        # so we need to handle both cases
        # get scheduler class name
        scheduler_class_name = self.noise_scheduler.__class__.__name__

        # todo handle if timestep is single value

        original_samples_chunks = torch.chunk(original_samples, original_samples.shape[0], dim=0)
        noise_chunks = torch.chunk(noise, noise.shape[0], dim=0)
        timesteps_chunks = torch.chunk(timesteps, timesteps.shape[0], dim=0)

        if len(timesteps_chunks) == 1 and len(timesteps_chunks) != len(original_samples_chunks):
            timesteps_chunks = [timesteps_chunks[0]] * len(original_samples_chunks)

        noisy_latents_chunks = []

        for idx in range(original_samples.shape[0]):

            # the add noise for ddpm solver is broken, do it ourselves
            noise_timesteps = timesteps_chunks[idx]
            if scheduler_class_name == 'DPMSolverMultistepScheduler':
                # Make sure sigmas and timesteps have the same device and dtype as original_samples
                sigmas = self.noise_scheduler.sigmas.to(device=original_samples_chunks[idx].device,
                                                        dtype=original_samples_chunks[idx].dtype)
                if original_samples_chunks[idx].device.type == "mps" and torch.is_floating_point(noise_timesteps):
                    # mps does not support float64
                    schedule_timesteps = self.noise_scheduler.timesteps.to(original_samples_chunks[idx].device,
                                                                           dtype=torch.float32)
                    noise_timesteps = noise_timesteps.to(original_samples_chunks[idx].device, dtype=torch.float32)
                else:
                    schedule_timesteps = self.noise_scheduler.timesteps.to(original_samples_chunks[idx].device)
                    noise_timesteps = noise_timesteps.to(original_samples_chunks[idx].device)

                step_indices = []
                for t in noise_timesteps:
                    for i, st in enumerate(schedule_timesteps):
                        if st == t:
                            step_indices.append(i)
                            break

                # find only first match. There can be double here, this breaks
                # step_indices = [(schedule_timesteps == t).nonzero().item() for t in timesteps]

                sigma = sigmas[step_indices].flatten()
                while len(sigma.shape) < len(original_samples.shape):
                    sigma = sigma.unsqueeze(-1)

                alpha_t, sigma_t = self.noise_scheduler._sigma_to_alpha_sigma_t(sigma)
                noisy_samples = alpha_t * original_samples + sigma_t * noise_chunks[idx]
                noisy_latents = noisy_samples
            else:
                noisy_latents = self.noise_scheduler.add_noise(original_samples_chunks[idx], noise_chunks[idx],
                                                               noise_timesteps)
            noisy_latents_chunks.append(noisy_latents)

        noisy_latents = torch.cat(noisy_latents_chunks, dim=0)
        return noisy_latents

    def predict_noise(
            self,
            latents: torch.Tensor,
            text_embeddings: Union[PromptEmbeds, None] = None,
            timestep: Union[int, torch.Tensor] = 1,
            guidance_scale=7.5,
            guidance_rescale=0,
            add_time_ids=None,
            conditional_embeddings: Union[PromptEmbeds, None] = None,
            unconditional_embeddings: Union[PromptEmbeds, None] = None,
            is_input_scaled=False,
            detach_unconditional=False,
            **kwargs,
    ):
        # get the embeddings
        if text_embeddings is None and conditional_embeddings is None:
            raise ValueError("Either text_embeddings or conditional_embeddings must be specified")
        if text_embeddings is None and unconditional_embeddings is not None:
            text_embeddings = concat_prompt_embeds([
                unconditional_embeddings,  # negative embedding
                conditional_embeddings,  # positive embedding
            ])
        elif text_embeddings is None and conditional_embeddings is not None:
            # not doing cfg
            text_embeddings = conditional_embeddings

        # CFG is comparing neg and positive, if we have concatenated embeddings
        # then we are doing it, otherwise we are not and takes half the time.
        do_classifier_free_guidance = True

        # check if batch size of embeddings matches batch size of latents
        if latents.shape[0] == text_embeddings.text_embeds.shape[0]:
            do_classifier_free_guidance = False
        elif latents.shape[0] * 2 != text_embeddings.text_embeds.shape[0]:
            raise ValueError("Batch size of latents must be the same or half the batch size of text embeddings")
        latents = latents.to(self.device_torch)
        text_embeddings = text_embeddings.to(self.device_torch)
        timestep = timestep.to(self.device_torch)

        # if timestep is zero dim, unsqueeze it
        if len(timestep.shape) == 0:
            timestep = timestep.unsqueeze(0)

        # if we only have 1 timestep, we can just use the same timestep for all
        if timestep.shape[0] == 1 and latents.shape[0] > 1:
            # check if it is rank 1 or 2
            if len(timestep.shape) == 1:
                timestep = timestep.repeat(latents.shape[0])
            else:
                timestep = timestep.repeat(latents.shape[0], 0)

        def scale_model_input(model_input, timestep_tensor):
            if is_input_scaled:
                return model_input
            mi_chunks = torch.chunk(model_input, model_input.shape[0], dim=0)
            timestep_chunks = torch.chunk(timestep_tensor, timestep_tensor.shape[0], dim=0)
            out_chunks = []
            # unsqueeze if timestep is zero dim
            for idx in range(model_input.shape[0]):
                # if scheduler has step_index
                if hasattr(self.noise_scheduler, '_step_index'):
                    self.noise_scheduler._step_index = None
                out_chunks.append(
                    self.noise_scheduler.scale_model_input(mi_chunks[idx], timestep_chunks[idx])
                )
            return torch.cat(out_chunks, dim=0)

        if self.is_xl:
            with torch.no_grad():
                # 16, 6 for bs of 4
                if add_time_ids is None:
                    add_time_ids = self.get_time_ids_from_latents(latents)

                    if do_classifier_free_guidance:
                        # todo check this with larget batches
                        add_time_ids = torch.cat([add_time_ids] * 2)

                if do_classifier_free_guidance:
                    latent_model_input = torch.cat([latents] * 2)
                    timestep = torch.cat([timestep] * 2)
                else:
                    latent_model_input = latents

                latent_model_input = scale_model_input(latent_model_input, timestep)

                added_cond_kwargs = {
                    # todo can we zero here the second text encoder? or match a blank string?
                    "text_embeds": text_embeddings.pooled_embeds,
                    "time_ids": add_time_ids,
                }

            if self.model_config.refiner_name_or_path is not None:
                # we have the refiner on the second half of everything. Do Both
                if do_classifier_free_guidance:
                    raise ValueError("Refiner is not supported with classifier free guidance")

                if self.unet.training:
                    input_chunks = torch.chunk(latent_model_input, 2, dim=0)
                    timestep_chunks = torch.chunk(timestep, 2, dim=0)
                    added_cond_kwargs_chunked = {
                        "text_embeds": torch.chunk(text_embeddings.pooled_embeds, 2, dim=0),
                        "time_ids": torch.chunk(add_time_ids, 2, dim=0),
                    }
                    text_embeds_chunks = torch.chunk(text_embeddings.text_embeds, 2, dim=0)

                    # predict the noise residual
                    base_pred = self.unet(
                        input_chunks[0],
                        timestep_chunks[0],
                        encoder_hidden_states=text_embeds_chunks[0],
                        added_cond_kwargs={
                            "text_embeds": added_cond_kwargs_chunked['text_embeds'][0],
                            "time_ids": added_cond_kwargs_chunked['time_ids'][0],
                        },
                        **kwargs,
                    ).sample

                    refiner_pred = self.refiner_unet(
                        input_chunks[1],
                        timestep_chunks[1],
                        encoder_hidden_states=text_embeds_chunks[1][:, :, -1280:],
                        # just use the first second text encoder
                        added_cond_kwargs={
                            "text_embeds": added_cond_kwargs_chunked['text_embeds'][1],
                            # "time_ids": added_cond_kwargs_chunked['time_ids'][1],
                            "time_ids": self.get_time_ids_from_latents(input_chunks[1], requires_aesthetic_score=True),
                        },
                        **kwargs,
                    ).sample

                    noise_pred = torch.cat([base_pred, refiner_pred], dim=0)
                else:
                    noise_pred = self.refiner_unet(
                        latent_model_input,
                        timestep,
                        encoder_hidden_states=text_embeddings.text_embeds[:, :, -1280:],
                        # just use the first second text encoder
                        added_cond_kwargs={
                            "text_embeds": text_embeddings.pooled_embeds,
                            "time_ids": self.get_time_ids_from_latents(latent_model_input,
                                                                       requires_aesthetic_score=True),
                        },
                        **kwargs,
                    ).sample

            else:

                # predict the noise residual
                noise_pred = self.unet(
                    latent_model_input.to(self.device_torch, self.torch_dtype),
                    timestep,
                    encoder_hidden_states=text_embeddings.text_embeds,
                    added_cond_kwargs=added_cond_kwargs,
                    **kwargs,
                ).sample

            if do_classifier_free_guidance:
                # perform guidance
                noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)
                noise_pred = noise_pred_uncond + guidance_scale * (
                        noise_pred_text - noise_pred_uncond
                )

                # https://github.com/huggingface/diffusers/blob/7a91ea6c2b53f94da930a61ed571364022b21044/src/diffusers/pipelines/stable_diffusion_xl/pipeline_stable_diffusion_xl.py#L775
                if guidance_rescale > 0.0:
                    # Based on 3.4. in https://arxiv.org/pdf/2305.08891.pdf
                    noise_pred = rescale_noise_cfg(noise_pred, noise_pred_text, guidance_rescale=guidance_rescale)

        else:
            with torch.no_grad():
                if do_classifier_free_guidance:
                    # if we are doing classifier free guidance, need to double up
                    latent_model_input = torch.cat([latents] * 2, dim=0)
                    timestep = torch.cat([timestep] * 2)
                else:
                    latent_model_input = latents

                latent_model_input = scale_model_input(latent_model_input, timestep)

                # check if we need to concat timesteps
                if isinstance(timestep, torch.Tensor) and len(timestep.shape) > 1:
                    ts_bs = timestep.shape[0]
                    if ts_bs != latent_model_input.shape[0]:
                        if ts_bs == 1:
                            timestep = torch.cat([timestep] * latent_model_input.shape[0])
                        elif ts_bs * 2 == latent_model_input.shape[0]:
                            timestep = torch.cat([timestep] * 2, dim=0)
                        else:
                            raise ValueError(
                                f"Batch size of latents {latent_model_input.shape[0]} must be the same or half the batch size of timesteps {timestep.shape[0]}")

            # predict the noise residual
            noise_pred = self.unet(
                latent_model_input.to(self.device_torch, self.torch_dtype),
                timestep,
                encoder_hidden_states=text_embeddings.text_embeds,
                **kwargs,
            ).sample

            if do_classifier_free_guidance:
                # perform guidance
                noise_pred_uncond, noise_pred_text = noise_pred.chunk(2, dim=0)
                if detach_unconditional:
                    noise_pred_uncond = noise_pred_uncond.detach()
                noise_pred = noise_pred_uncond + guidance_scale * (
                        noise_pred_text - noise_pred_uncond
                )

                # https://github.com/huggingface/diffusers/blob/7a91ea6c2b53f94da930a61ed571364022b21044/src/diffusers/pipelines/stable_diffusion_xl/pipeline_stable_diffusion_xl.py#L775
                if guidance_rescale > 0.0:
                    # Based on 3.4. in https://arxiv.org/pdf/2305.08891.pdf
                    noise_pred = rescale_noise_cfg(noise_pred, noise_pred_text, guidance_rescale=guidance_rescale)

        return noise_pred

    def step_scheduler(self, model_input, latent_input, timestep_tensor, noise_scheduler=None):
        if noise_scheduler is None:
            noise_scheduler = self.noise_scheduler
        # // sometimes they are on the wrong device, no idea why
        if isinstance(noise_scheduler, DDPMScheduler) or isinstance(noise_scheduler, LCMScheduler):
            try:
                noise_scheduler.betas = noise_scheduler.betas.to(self.device_torch)
                noise_scheduler.alphas = noise_scheduler.alphas.to(self.device_torch)
                noise_scheduler.alphas_cumprod = noise_scheduler.alphas_cumprod.to(self.device_torch)
            except Exception as e:
                pass

        mi_chunks = torch.chunk(model_input, model_input.shape[0], dim=0)
        latent_chunks = torch.chunk(latent_input, latent_input.shape[0], dim=0)
        timestep_chunks = torch.chunk(timestep_tensor, timestep_tensor.shape[0], dim=0)
        out_chunks = []
        if len(timestep_chunks) == 1 and len(mi_chunks) > 1:
            # expand timestep to match
            timestep_chunks = timestep_chunks * len(mi_chunks)

        for idx in range(model_input.shape[0]):
            # Reset it so it is unique for the
            if hasattr(noise_scheduler, '_step_index'):
                noise_scheduler._step_index = None
            if hasattr(noise_scheduler, 'is_scale_input_called'):
                noise_scheduler.is_scale_input_called = True
            out_chunks.append(
                noise_scheduler.step(mi_chunks[idx], timestep_chunks[idx], latent_chunks[idx], return_dict=False)[
                    0]
            )
        return torch.cat(out_chunks, dim=0)

    # ref: https://github.com/huggingface/diffusers/blob/0bab447670f47c28df60fbd2f6a0f833f75a16f5/src/diffusers/pipelines/stable_diffusion/pipeline_stable_diffusion.py#L746
    def diffuse_some_steps(
            self,
            latents: torch.FloatTensor,
            text_embeddings: PromptEmbeds,
            total_timesteps: int = 1000,
            start_timesteps=0,
            guidance_scale=1,
            add_time_ids=None,
            bleed_ratio: float = 0.5,
            bleed_latents: torch.FloatTensor = None,
            is_input_scaled=False,
            **kwargs,
    ):
        timesteps_to_run = self.noise_scheduler.timesteps[start_timesteps:total_timesteps]

        for timestep in tqdm(timesteps_to_run, leave=False):
            timestep = timestep.unsqueeze_(0)
            noise_pred = self.predict_noise(
                latents,
                text_embeddings,
                timestep,
                guidance_scale=guidance_scale,
                add_time_ids=add_time_ids,
                is_input_scaled=is_input_scaled,
                **kwargs,
            )
            # some schedulers need to run separately, so do that. (euler for example)

            latents = self.step_scheduler(noise_pred, latents, timestep)

            # if not last step, and bleeding, bleed in some latents
            if bleed_latents is not None and timestep != self.noise_scheduler.timesteps[-1]:
                latents = (latents * (1 - bleed_ratio)) + (bleed_latents * bleed_ratio)

            # only skip first scaling
            is_input_scaled = False

        # return latents_steps
        return latents

    def encode_prompt(
            self,
            prompt,
            prompt2=None,
            num_images_per_prompt=1,
            force_all=False,
            long_prompts=False,
            max_length=None,
            dropout_prob=0.0,
    ) -> PromptEmbeds:
        # sd1.5 embeddings are (bs, 77, 768)
        prompt = prompt
        # if it is not a list, make it one
        if not isinstance(prompt, list):
            prompt = [prompt]

        if prompt2 is not None and not isinstance(prompt2, list):
            prompt2 = [prompt2]
        if self.is_xl:
            # todo make this a config
            # 50% chance to use an encoder anyway even if it is disabled
            # allows the other TE to compensate for the disabled one
            # use_encoder_1 = self.use_text_encoder_1 or force_all or random.random() > 0.5
            # use_encoder_2 = self.use_text_encoder_2 or force_all or random.random() > 0.5
            use_encoder_1 = True
            use_encoder_2 = True

            return PromptEmbeds(
                train_tools.encode_prompts_xl(
                    self.tokenizer,
                    self.text_encoder,
                    prompt,
                    prompt2,
                    num_images_per_prompt=num_images_per_prompt,
                    use_text_encoder_1=use_encoder_1,
                    use_text_encoder_2=use_encoder_2,
                    truncate=not long_prompts,
                    max_length=max_length,
                    dropout_prob=dropout_prob,
                )
            )
        else:
            return PromptEmbeds(
                train_tools.encode_prompts(
                    self.tokenizer,
                    self.text_encoder,
                    prompt,
                    truncate=not long_prompts,
                    max_length=max_length,
                    dropout_prob=dropout_prob
                )
            )

    @torch.no_grad()
    def encode_images(
            self,
            image_list: List[torch.Tensor],
            device=None,
            dtype=None
    ):
        if device is None:
            device = self.device
        if dtype is None:
            dtype = self.torch_dtype

        latent_list = []
        # Move to vae to device if on cpu
        if self.vae.device == 'cpu':
            self.vae.to(self.device)
        self.vae.eval()
        self.vae.requires_grad_(False)
        # move to device and dtype
        image_list = [image.to(self.device, dtype=self.torch_dtype) for image in image_list]

        # resize images if not divisible by 8
        for i in range(len(image_list)):
            image = image_list[i]
            if image.shape[1] % 8 != 0 or image.shape[2] % 8 != 0:
                image_list[i] = Resize((image.shape[1] // 8 * 8, image.shape[2] // 8 * 8))(image)

        images = torch.stack(image_list)
        latents = self.vae.encode(images).latent_dist.sample()
        latents = latents * self.vae.config['scaling_factor']
        latents = latents.to(device, dtype=dtype)

        return latents

    def decode_latents(
            self,
            latents: torch.Tensor,
            device=None,
            dtype=None
    ):
        if device is None:
            device = self.device
        if dtype is None:
            dtype = self.torch_dtype

        # Move to vae to device if on cpu
        if self.vae.device == 'cpu':
            self.vae.to(self.device)
        latents = latents.to(device, dtype=dtype)
        latents = latents / self.vae.config['scaling_factor']
        images = self.vae.decode(latents).sample
        images = images.to(device, dtype=dtype)

        return images

    def encode_image_prompt_pairs(
            self,
            prompt_list: List[str],
            image_list: List[torch.Tensor],
            device=None,
            dtype=None
    ):
        # todo check image types and expand and rescale as needed
        # device and dtype are for outputs
        if device is None:
            device = self.device
        if dtype is None:
            dtype = self.torch_dtype

        embedding_list = []
        latent_list = []
        # embed the prompts
        for prompt in prompt_list:
            embedding = self.encode_prompt(prompt).to(self.device_torch, dtype=dtype)
            embedding_list.append(embedding)

        return embedding_list, latent_list

    def get_weight_by_name(self, name):
        # weights begin with te{te_num}_ for text encoder
        # weights begin with unet_ for unet_
        if name.startswith('te'):
            key = name[4:]
            # text encoder
            te_num = int(name[2])
            if isinstance(self.text_encoder, list):
                return self.text_encoder[te_num].state_dict()[key]
            else:
                return self.text_encoder.state_dict()[key]
        elif name.startswith('unet'):
            key = name[5:]
            # unet
            return self.unet.state_dict()[key]

        raise ValueError(f"Unknown weight name: {name}")

    def inject_trigger_into_prompt(self, prompt, trigger=None, to_replace_list=None, add_if_not_present=False):
        return inject_trigger_into_prompt(
            prompt,
            trigger=trigger,
            to_replace_list=to_replace_list,
            add_if_not_present=add_if_not_present,
        )

    def state_dict(self, vae=True, text_encoder=True, unet=True):
        state_dict = OrderedDict()
        if vae:
            for k, v in self.vae.state_dict().items():
                new_key = k if k.startswith(f"{SD_PREFIX_VAE}") else f"{SD_PREFIX_VAE}_{k}"
                state_dict[new_key] = v
        if text_encoder:
            if isinstance(self.text_encoder, list):
                for i, encoder in enumerate(self.text_encoder):
                    for k, v in encoder.state_dict().items():
                        new_key = k if k.startswith(
                            f"{SD_PREFIX_TEXT_ENCODER}{i}_") else f"{SD_PREFIX_TEXT_ENCODER}{i}_{k}"
                        state_dict[new_key] = v
            else:
                for k, v in self.text_encoder.state_dict().items():
                    new_key = k if k.startswith(f"{SD_PREFIX_TEXT_ENCODER}_") else f"{SD_PREFIX_TEXT_ENCODER}_{k}"
                    state_dict[new_key] = v
        if unet:
            for k, v in self.unet.state_dict().items():
                new_key = k if k.startswith(f"{SD_PREFIX_UNET}_") else f"{SD_PREFIX_UNET}_{k}"
                state_dict[new_key] = v
        return state_dict

    def named_parameters(self, vae=True, text_encoder=True, unet=True, refiner=False, state_dict_keys=False) -> \
            OrderedDict[
                str, Parameter]:
        named_params: OrderedDict[str, Parameter] = OrderedDict()
        if vae:
            for name, param in self.vae.named_parameters(recurse=True, prefix=f"{SD_PREFIX_VAE}"):
                named_params[name] = param
        if text_encoder:
            if isinstance(self.text_encoder, list):
                for i, encoder in enumerate(self.text_encoder):
                    if self.is_xl and not self.model_config.use_text_encoder_1 and i == 0:
                        # dont add these params
                        continue
                    if self.is_xl and not self.model_config.use_text_encoder_2 and i == 1:
                        # dont add these params
                        continue

                    for name, param in encoder.named_parameters(recurse=True, prefix=f"{SD_PREFIX_TEXT_ENCODER}{i}"):
                        named_params[name] = param
            else:
                for name, param in self.text_encoder.named_parameters(recurse=True, prefix=f"{SD_PREFIX_TEXT_ENCODER}"):
                    named_params[name] = param
        if unet:
            for name, param in self.unet.named_parameters(recurse=True, prefix=f"{SD_PREFIX_UNET}"):
                named_params[name] = param

        if refiner:
            for name, param in self.refiner_unet.named_parameters(recurse=True, prefix=f"{SD_PREFIX_REFINER_UNET}"):
                named_params[name] = param

        # convert to state dict keys, jsut replace . with _ on keys
        if state_dict_keys:
            new_named_params = OrderedDict()
            for k, v in named_params.items():
                # replace only the first . with an _
                new_key = k.replace('.', '_', 1)
                new_named_params[new_key] = v
            named_params = new_named_params

        return named_params

    def save_refiner(self, output_file: str, meta: OrderedDict, save_dtype=get_torch_dtype('fp16')):

        # load the full refiner since we only train unet
        if self.model_config.refiner_name_or_path is None:
            raise ValueError("Refiner must be specified to save it")
        refiner_config_path = os.path.join(ORIG_CONFIGS_ROOT, 'sd_xl_refiner.yaml')
        # load the refiner model
        dtype = get_torch_dtype(self.dtype)
        model_path = self.model_config._original_refiner_name_or_path
        if not os.path.exists(model_path) or os.path.isdir(model_path):
            # TODO only load unet??
            refiner = StableDiffusionXLImg2ImgPipeline.from_pretrained(
                model_path,
                dtype=dtype,
                device='cpu',
                variant="fp16",
                use_safetensors=True,
            )
        else:
            refiner = StableDiffusionXLImg2ImgPipeline.from_single_file(
                model_path,
                dtype=dtype,
                device='cpu',
                torch_dtype=self.torch_dtype,
                original_config_file=refiner_config_path,
            )
        # replace original unet
        refiner.unet = self.refiner_unet
        flush()

        diffusers_state_dict = OrderedDict()
        for k, v in refiner.vae.state_dict().items():
            new_key = k if k.startswith(f"{SD_PREFIX_VAE}") else f"{SD_PREFIX_VAE}_{k}"
            diffusers_state_dict[new_key] = v
        for k, v in refiner.text_encoder_2.state_dict().items():
            new_key = k if k.startswith(f"{SD_PREFIX_TEXT_ENCODER2}_") else f"{SD_PREFIX_TEXT_ENCODER2}_{k}"
            diffusers_state_dict[new_key] = v
        for k, v in refiner.unet.state_dict().items():
            new_key = k if k.startswith(f"{SD_PREFIX_UNET}_") else f"{SD_PREFIX_UNET}_{k}"
            diffusers_state_dict[new_key] = v

        converted_state_dict = get_ldm_state_dict_from_diffusers(
            diffusers_state_dict,
            'sdxl_refiner',
            device='cpu',
            dtype=save_dtype
        )

        # make sure parent folder exists
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        save_file(converted_state_dict, output_file, metadata=meta)

        if self.config_file is not None:
            output_path_no_ext = os.path.splitext(output_file)[0]
            output_config_path = f"{output_path_no_ext}.yaml"
            shutil.copyfile(self.config_file, output_config_path)

    def save(self, output_file: str, meta: OrderedDict, save_dtype=get_torch_dtype('fp16'), logit_scale=None):
        version_string = '1'
        if self.is_v2:
            version_string = '2'
        if self.is_xl:
            version_string = 'sdxl'
        if self.is_ssd:
            # overwrite sdxl because both wil be true here
            version_string = 'ssd'
        if self.is_ssd and self.is_vega:
            version_string = 'vega'
        # if output file does not end in .safetensors, then it is a directory and we are
        # saving in diffusers format
        if not output_file.endswith('.safetensors'):
            # diffusers
            self.pipeline.save_pretrained(
                save_directory=output_file,
                safe_serialization=True,
            )
            # save out meta config
            meta_path = os.path.join(output_file, 'aitk_meta.yaml')
            with open(meta_path, 'w') as f:
                yaml.dump(meta, f)

        else:
            save_ldm_model_from_diffusers(
                sd=self,
                output_file=output_file,
                meta=meta,
                save_dtype=save_dtype,
                sd_version=version_string,
            )
            if self.config_file is not None:
                output_path_no_ext = os.path.splitext(output_file)[0]
                output_config_path = f"{output_path_no_ext}.yaml"
                shutil.copyfile(self.config_file, output_config_path)

    def prepare_optimizer_params(
            self,
            unet=False,
            text_encoder=False,
            text_encoder_lr=None,
            unet_lr=None,
            refiner_lr=None,
            refiner=False,
            default_lr=1e-6,
    ):
        # todo maybe only get locon ones?
        # not all items are saved, to make it match, we need to match out save mappings
        # and not train anything not mapped. Also add learning rate
        version = 'sd1'
        if self.is_xl:
            version = 'sdxl'
        if self.is_v2:
            version = 'sd2'
        mapping_filename = f"stable_diffusion_{version}.json"
        mapping_path = os.path.join(KEYMAPS_ROOT, mapping_filename)
        with open(mapping_path, 'r') as f:
            mapping = json.load(f)
        ldm_diffusers_keymap = mapping['ldm_diffusers_keymap']

        trainable_parameters = []

        # we use state dict to find params

        if unet:
            named_params = self.named_parameters(vae=False, unet=unet, text_encoder=False, state_dict_keys=True)
            unet_lr = unet_lr if unet_lr is not None else default_lr
            params = []
            for key, diffusers_key in ldm_diffusers_keymap.items():
                if diffusers_key in named_params and diffusers_key not in DO_NOT_TRAIN_WEIGHTS:
                    if named_params[diffusers_key].requires_grad:
                        params.append(named_params[diffusers_key])
            param_data = {"params": params, "lr": unet_lr}
            trainable_parameters.append(param_data)
            print(f"Found {len(params)} trainable parameter in unet")

        if text_encoder:
            named_params = self.named_parameters(vae=False, unet=False, text_encoder=text_encoder, state_dict_keys=True)
            text_encoder_lr = text_encoder_lr if text_encoder_lr is not None else default_lr
            params = []
            for key, diffusers_key in ldm_diffusers_keymap.items():
                if diffusers_key in named_params and diffusers_key not in DO_NOT_TRAIN_WEIGHTS:
                    if named_params[diffusers_key].requires_grad:
                        params.append(named_params[diffusers_key])
            param_data = {"params": params, "lr": text_encoder_lr}
            trainable_parameters.append(param_data)

            print(f"Found {len(params)} trainable parameter in text encoder")

        if refiner:
            named_params = self.named_parameters(vae=False, unet=False, text_encoder=False, refiner=True,
                                                 state_dict_keys=True)
            refiner_lr = refiner_lr if refiner_lr is not None else default_lr
            params = []
            for key, diffusers_key in ldm_diffusers_keymap.items():
                diffusers_key = f"refiner_{diffusers_key}"
                if diffusers_key in named_params and diffusers_key not in DO_NOT_TRAIN_WEIGHTS:
                    if named_params[diffusers_key].requires_grad:
                        params.append(named_params[diffusers_key])
            param_data = {"params": params, "lr": refiner_lr}
            trainable_parameters.append(param_data)

            print(f"Found {len(params)} trainable parameter in refiner")

        return trainable_parameters

    def save_device_state(self):
        # saves the current device state for all modules
        # this is useful for when we want to alter the state and restore it
        self.device_state = {
            **empty_preset,
            'vae': {
                'training': self.vae.training,
                'device': self.vae.device,
            },
            'unet': {
                'training': self.unet.training,
                'device': self.unet.device,
                'requires_grad': self.unet.conv_in.weight.requires_grad,
            },
        }
        if isinstance(self.text_encoder, list):
            self.device_state['text_encoder']: List[dict] = []
            for encoder in self.text_encoder:
                self.device_state['text_encoder'].append({
                    'training': encoder.training,
                    'device': encoder.device,
                    # todo there has to be a better way to do this
                    'requires_grad': encoder.text_model.final_layer_norm.weight.requires_grad
                })
        else:
            self.device_state['text_encoder'] = {
                'training': self.text_encoder.training,
                'device': self.text_encoder.device,
                'requires_grad': self.text_encoder.text_model.final_layer_norm.weight.requires_grad
            }
        if self.adapter is not None:
            if isinstance(self.adapter, IPAdapter):
                requires_grad = self.adapter.image_proj_model.training
                adapter_device = self.unet.device
            elif isinstance(self.adapter, T2IAdapter):
                requires_grad = self.adapter.adapter.conv_in.weight.requires_grad
                adapter_device = self.adapter.device
            elif isinstance(self.adapter, ClipVisionAdapter):
                requires_grad = self.adapter.embedder.training
                adapter_device = self.adapter.device
            elif isinstance(self.adapter, CustomAdapter):
                requires_grad = self.adapter.training
                adapter_device = self.adapter.device
            elif isinstance(self.adapter, ReferenceAdapter):
                # todo update this!!
                requires_grad = True
                adapter_device = self.adapter.device
            else:
                raise ValueError(f"Unknown adapter type: {type(self.adapter)}")
            self.device_state['adapter'] = {
                'training': self.adapter.training,
                'device': adapter_device,
                'requires_grad': requires_grad,
            }

        if self.refiner_unet is not None:
            self.device_state['refiner_unet'] = {
                'training': self.refiner_unet.training,
                'device': self.refiner_unet.device,
                'requires_grad': self.refiner_unet.conv_in.weight.requires_grad,
            }

    def restore_device_state(self):
        # restores the device state for all modules
        # this is useful for when we want to alter the state and restore it
        if self.device_state is None:
            return
        self.set_device_state(self.device_state)
        self.device_state = None

    def set_device_state(self, state):
        if state['vae']['training']:
            self.vae.train()
        else:
            self.vae.eval()
        self.vae.to(state['vae']['device'])
        if state['unet']['training']:
            self.unet.train()
        else:
            self.unet.eval()
        self.unet.to(state['unet']['device'])
        if state['unet']['requires_grad']:
            self.unet.requires_grad_(True)
        else:
            self.unet.requires_grad_(False)
        if isinstance(self.text_encoder, list):
            for i, encoder in enumerate(self.text_encoder):
                if isinstance(state['text_encoder'], list):
                    if state['text_encoder'][i]['training']:
                        encoder.train()
                    else:
                        encoder.eval()
                    encoder.to(state['text_encoder'][i]['device'])
                    encoder.requires_grad_(state['text_encoder'][i]['requires_grad'])
                else:
                    if state['text_encoder']['training']:
                        encoder.train()
                    else:
                        encoder.eval()
                    encoder.to(state['text_encoder']['device'])
                    encoder.requires_grad_(state['text_encoder']['requires_grad'])
        else:
            if state['text_encoder']['training']:
                self.text_encoder.train()
            else:
                self.text_encoder.eval()
            self.text_encoder.to(state['text_encoder']['device'])
            self.text_encoder.requires_grad_(state['text_encoder']['requires_grad'])

        if self.adapter is not None:
            self.adapter.to(state['adapter']['device'])
            self.adapter.requires_grad_(state['adapter']['requires_grad'])
            if state['adapter']['training']:
                self.adapter.train()
            else:
                self.adapter.eval()

        if self.refiner_unet is not None:
            self.refiner_unet.to(state['refiner_unet']['device'])
            self.refiner_unet.requires_grad_(state['refiner_unet']['requires_grad'])
            if state['refiner_unet']['training']:
                self.refiner_unet.train()
            else:
                self.refiner_unet.eval()
        flush()

    def set_device_state_preset(self, device_state_preset: DeviceStatePreset):
        # sets a preset for device state

        # save current state first
        self.save_device_state()

        active_modules = []
        training_modules = []
        if device_state_preset in ['cache_latents']:
            active_modules = ['vae']
        if device_state_preset in ['generate']:
            active_modules = ['vae', 'unet', 'text_encoder', 'adapter', 'refiner_unet']

        state = copy.deepcopy(empty_preset)
        # vae
        state['vae'] = {
            'training': 'vae' in training_modules,
            'device': self.device_torch if 'vae' in active_modules else 'cpu',
            'requires_grad': 'vae' in training_modules,
        }

        # unet
        state['unet'] = {
            'training': 'unet' in training_modules,
            'device': self.device_torch if 'unet' in active_modules else 'cpu',
            'requires_grad': 'unet' in training_modules,
        }

        if self.refiner_unet is not None:
            state['refiner_unet'] = {
                'training': 'refiner_unet' in training_modules,
                'device': self.device_torch if 'refiner_unet' in active_modules else 'cpu',
                'requires_grad': 'refiner_unet' in training_modules,
            }

        # text encoder
        if isinstance(self.text_encoder, list):
            state['text_encoder'] = []
            for i, encoder in enumerate(self.text_encoder):
                state['text_encoder'].append({
                    'training': 'text_encoder' in training_modules,
                    'device': self.device_torch if 'text_encoder' in active_modules else 'cpu',
                    'requires_grad': 'text_encoder' in training_modules,
                })
        else:
            state['text_encoder'] = {
                'training': 'text_encoder' in training_modules,
                'device': self.device_torch if 'text_encoder' in active_modules else 'cpu',
                'requires_grad': 'text_encoder' in training_modules,
            }

        if self.adapter is not None:
            state['adapter'] = {
                'training': 'adapter' in training_modules,
                'device': self.device_torch if 'adapter' in active_modules else 'cpu',
                'requires_grad': 'adapter' in training_modules,
            }

        self.set_device_state(state)
