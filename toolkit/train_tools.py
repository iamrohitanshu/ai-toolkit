import argparse
import hashlib
import json
import os
import time
from typing import TYPE_CHECKING, Union
import sys

from torch.cuda.amp import GradScaler

from toolkit.paths import SD_SCRIPTS_ROOT

sys.path.append(SD_SCRIPTS_ROOT)

from diffusers import (
    StableDiffusionPipeline,
    DDPMScheduler,
    EulerAncestralDiscreteScheduler,
    DPMSolverMultistepScheduler,
    DPMSolverSinglestepScheduler,
    LMSDiscreteScheduler,
    PNDMScheduler,
    DDIMScheduler,
    EulerDiscreteScheduler,
    HeunDiscreteScheduler,
    KDPM2DiscreteScheduler,
    KDPM2AncestralDiscreteScheduler,
)
from library.lpw_stable_diffusion import StableDiffusionLongPromptWeightingPipeline
import torch
import re

SCHEDULER_LINEAR_START = 0.00085
SCHEDULER_LINEAR_END = 0.0120
SCHEDULER_TIMESTEPS = 1000
SCHEDLER_SCHEDULE = "scaled_linear"

UNET_ATTENTION_TIME_EMBED_DIM = 256  # XL
TEXT_ENCODER_2_PROJECTION_DIM = 1280
UNET_PROJECTION_CLASS_EMBEDDING_INPUT_DIM = 2816


def get_torch_dtype(dtype_str):
    # if it is a torch dtype, return it
    if isinstance(dtype_str, torch.dtype):
        return dtype_str
    if dtype_str == "float" or dtype_str == "fp32" or dtype_str == "single" or dtype_str == "float32":
        return torch.float
    if dtype_str == "fp16" or dtype_str == "half" or dtype_str == "float16":
        return torch.float16
    if dtype_str == "bf16" or dtype_str == "bfloat16":
        return torch.bfloat16
    return dtype_str


def replace_filewords_prompt(prompt, args: argparse.Namespace):
    # if name_replace attr in args (may not be)
    if hasattr(args, "name_replace") and args.name_replace is not None:
        # replace [name] to args.name_replace
        prompt = prompt.replace("[name]", args.name_replace)
    if hasattr(args, "prepend") and args.prepend is not None:
        # prepend to every item in prompt file
        prompt = args.prepend + ' ' + prompt
    if hasattr(args, "append") and args.append is not None:
        # append to every item in prompt file
        prompt = prompt + ' ' + args.append
    return prompt


def replace_filewords_in_dataset_group(dataset_group, args: argparse.Namespace):
    # if name_replace attr in args (may not be)
    if hasattr(args, "name_replace") and args.name_replace is not None:
        if not len(dataset_group.image_data) > 0:
            # throw error
            raise ValueError("dataset_group.image_data is empty")
        for key in dataset_group.image_data:
            dataset_group.image_data[key].caption = dataset_group.image_data[key].caption.replace(
                "[name]", args.name_replace)

    return dataset_group


def get_seeds_from_latents(latents):
    # latents shape = (batch_size, 4, height, width)
    # for speed we only use 8x8 slice of the first channel
    seeds = []

    # split batch up
    for i in range(latents.shape[0]):
        # use only first channel, multiply by 255 and convert to int
        tensor = latents[i, 0, :, :] * 255.0  # shape = (height, width)
        # slice 8x8
        tensor = tensor[:8, :8]
        # clip to 0-255
        tensor = torch.clamp(tensor, 0, 255)
        # convert to 8bit int
        tensor = tensor.to(torch.uint8)
        # convert to bytes
        tensor_bytes = tensor.cpu().numpy().tobytes()
        # hash
        hash_object = hashlib.sha256(tensor_bytes)
        # get hex
        hex_dig = hash_object.hexdigest()
        # convert to int
        seed = int(hex_dig, 16) % (2 ** 32)
        # append
        seeds.append(seed)
    return seeds


def get_noise_from_latents(latents):
    seed_list = get_seeds_from_latents(latents)
    noise = []
    for seed in seed_list:
        torch.manual_seed(seed)
        torch.cuda.manual_seed(seed)
        noise.append(torch.randn_like(latents[0]))
    return torch.stack(noise)


# mix 0 is completely noise mean, mix 1 is completely target mean

def match_noise_to_target_mean_offset(noise, target, mix=0.5, dim=None):
    dim = dim or (1, 2, 3)
    # reduce mean of noise on dim 2, 3, keeping 0 and 1 intact
    noise_mean = noise.mean(dim=dim, keepdim=True)
    target_mean = target.mean(dim=dim, keepdim=True)

    new_noise_mean = mix * target_mean + (1 - mix) * noise_mean

    noise = noise - noise_mean + new_noise_mean
    return noise


def sample_images(
        accelerator,
        args: argparse.Namespace,
        epoch,
        steps,
        device,
        vae,
        tokenizer,
        text_encoder,
        unet,
        prompt_replacement=None,
        force_sample=False
):
    """
    StableDiffusionLongPromptWeightingPipelineの改造版を使うようにしたので、clip skipおよびプロンプトの重みづけに対応した
    """
    if not force_sample:
        if args.sample_every_n_steps is None and args.sample_every_n_epochs is None:
            return
        if args.sample_every_n_epochs is not None:
            # sample_every_n_steps は無視する
            if epoch is None or epoch % args.sample_every_n_epochs != 0:
                return
        else:
            if steps % args.sample_every_n_steps != 0 or epoch is not None:  # steps is not divisible or end of epoch
                return

    is_sample_only = args.sample_only
    is_generating_only = hasattr(args, "is_generating_only") and args.is_generating_only

    print(f"\ngenerating sample images at step / サンプル画像生成 ステップ: {steps}")
    if not os.path.isfile(args.sample_prompts):
        print(f"No prompt file / プロンプトファイルがありません: {args.sample_prompts}")
        return

    org_vae_device = vae.device  # CPUにいるはず
    vae.to(device)

    # read prompts

    # with open(args.sample_prompts, "rt", encoding="utf-8") as f:
    #     prompts = f.readlines()

    if args.sample_prompts.endswith(".txt"):
        with open(args.sample_prompts, "r", encoding="utf-8") as f:
            lines = f.readlines()
        prompts = [line.strip() for line in lines if len(line.strip()) > 0 and line[0] != "#"]
    elif args.sample_prompts.endswith(".json"):
        with open(args.sample_prompts, "r", encoding="utf-8") as f:
            prompts = json.load(f)

    # schedulerを用意する
    sched_init_args = {}
    if args.sample_sampler == "ddim":
        scheduler_cls = DDIMScheduler
    elif args.sample_sampler == "ddpm":  # ddpmはおかしくなるのでoptionから外してある
        scheduler_cls = DDPMScheduler
    elif args.sample_sampler == "pndm":
        scheduler_cls = PNDMScheduler
    elif args.sample_sampler == "lms" or args.sample_sampler == "k_lms":
        scheduler_cls = LMSDiscreteScheduler
    elif args.sample_sampler == "euler" or args.sample_sampler == "k_euler":
        scheduler_cls = EulerDiscreteScheduler
    elif args.sample_sampler == "euler_a" or args.sample_sampler == "k_euler_a":
        scheduler_cls = EulerAncestralDiscreteScheduler
    elif args.sample_sampler == "dpmsolver" or args.sample_sampler == "dpmsolver++":
        scheduler_cls = DPMSolverMultistepScheduler
        sched_init_args["algorithm_type"] = args.sample_sampler
    elif args.sample_sampler == "dpmsingle":
        scheduler_cls = DPMSolverSinglestepScheduler
    elif args.sample_sampler == "heun":
        scheduler_cls = HeunDiscreteScheduler
    elif args.sample_sampler == "dpm_2" or args.sample_sampler == "k_dpm_2":
        scheduler_cls = KDPM2DiscreteScheduler
    elif args.sample_sampler == "dpm_2_a" or args.sample_sampler == "k_dpm_2_a":
        scheduler_cls = KDPM2AncestralDiscreteScheduler
    else:
        scheduler_cls = DDIMScheduler

    if args.v_parameterization:
        sched_init_args["prediction_type"] = "v_prediction"

    scheduler = scheduler_cls(
        num_train_timesteps=SCHEDULER_TIMESTEPS,
        beta_start=SCHEDULER_LINEAR_START,
        beta_end=SCHEDULER_LINEAR_END,
        beta_schedule=SCHEDLER_SCHEDULE,
        **sched_init_args,
    )

    # clip_sample=Trueにする
    if hasattr(scheduler.config, "clip_sample") and scheduler.config.clip_sample is False:
        # print("set clip_sample to True")
        scheduler.config.clip_sample = True

    pipeline = StableDiffusionLongPromptWeightingPipeline(
        text_encoder=text_encoder,
        vae=vae,
        unet=unet,
        tokenizer=tokenizer,
        scheduler=scheduler,
        clip_skip=args.clip_skip,
        safety_checker=None,
        feature_extractor=None,
        requires_safety_checker=False,
    )
    pipeline.to(device)

    if is_generating_only:
        save_dir = args.output_dir
    else:
        save_dir = args.output_dir + "/sample"
    os.makedirs(save_dir, exist_ok=True)

    rng_state = torch.get_rng_state()
    cuda_rng_state = torch.cuda.get_rng_state() if torch.cuda.is_available() else None

    with torch.no_grad():
        with accelerator.autocast():
            for i, prompt in enumerate(prompts):
                if not accelerator.is_main_process:
                    continue

                if isinstance(prompt, dict):
                    negative_prompt = prompt.get("negative_prompt")
                    sample_steps = prompt.get("sample_steps", 30)
                    width = prompt.get("width", 512)
                    height = prompt.get("height", 512)
                    scale = prompt.get("scale", 7.5)
                    seed = prompt.get("seed")
                    prompt = prompt.get("prompt")

                    prompt = replace_filewords_prompt(prompt, args)
                    negative_prompt = replace_filewords_prompt(negative_prompt, args)
                else:
                    prompt = replace_filewords_prompt(prompt, args)
                    # prompt = prompt.strip()
                    # if len(prompt) == 0 or prompt[0] == "#":
                    #     continue

                    # subset of gen_img_diffusers
                    prompt_args = prompt.split(" --")
                    prompt = prompt_args[0]
                    negative_prompt = None
                    sample_steps = 30
                    width = height = 512
                    scale = 7.5
                    seed = None
                    for parg in prompt_args:
                        try:
                            m = re.match(r"w (\d+)", parg, re.IGNORECASE)
                            if m:
                                width = int(m.group(1))
                                continue

                            m = re.match(r"h (\d+)", parg, re.IGNORECASE)
                            if m:
                                height = int(m.group(1))
                                continue

                            m = re.match(r"d (\d+)", parg, re.IGNORECASE)
                            if m:
                                seed = int(m.group(1))
                                continue

                            m = re.match(r"s (\d+)", parg, re.IGNORECASE)
                            if m:  # steps
                                sample_steps = max(1, min(1000, int(m.group(1))))
                                continue

                            m = re.match(r"l ([\d\.]+)", parg, re.IGNORECASE)
                            if m:  # scale
                                scale = float(m.group(1))
                                continue

                            m = re.match(r"n (.+)", parg, re.IGNORECASE)
                            if m:  # negative prompt
                                negative_prompt = m.group(1)
                                continue

                        except ValueError as ex:
                            print(f"Exception in parsing / 解析エラー: {parg}")
                            print(ex)

                if seed is not None:
                    torch.manual_seed(seed)
                    torch.cuda.manual_seed(seed)

                if prompt_replacement is not None:
                    prompt = prompt.replace(prompt_replacement[0], prompt_replacement[1])
                    if negative_prompt is not None:
                        negative_prompt = negative_prompt.replace(prompt_replacement[0], prompt_replacement[1])

                height = max(64, height - height % 8)  # round to divisible by 8
                width = max(64, width - width % 8)  # round to divisible by 8
                print(f"prompt: {prompt}")
                print(f"negative_prompt: {negative_prompt}")
                print(f"height: {height}")
                print(f"width: {width}")
                print(f"sample_steps: {sample_steps}")
                print(f"scale: {scale}")
                image = pipeline(
                    prompt=prompt,
                    height=height,
                    width=width,
                    num_inference_steps=sample_steps,
                    guidance_scale=scale,
                    negative_prompt=negative_prompt,
                ).images[0]

                ts_str = time.strftime("%Y%m%d%H%M%S", time.localtime())
                num_suffix = f"e{epoch:06d}" if epoch is not None else f"{steps:06d}"
                seed_suffix = "" if seed is None else f"_{seed}"

                if is_generating_only:
                    img_filename = (
                        f"{'' if args.output_name is None else args.output_name + '_'}{ts_str}_{num_suffix}_{i:02d}{seed_suffix}.png"
                    )
                else:
                    img_filename = (
                        f"{'' if args.output_name is None else args.output_name + '_'}{ts_str}_{i:04d}{seed_suffix}.png"
                    )
                if is_sample_only:
                    # make prompt txt file
                    img_path_no_ext = os.path.join(save_dir, img_filename[:-4])
                    with open(img_path_no_ext + ".txt", "w") as f:
                        # put prompt in txt file
                        f.write(prompt)
                        # close file
                        f.close()

                image.save(os.path.join(save_dir, img_filename))

                # wandb有効時のみログを送信
                try:
                    wandb_tracker = accelerator.get_tracker("wandb")
                    try:
                        import wandb
                    except ImportError:  # 事前に一度確認するのでここはエラー出ないはず
                        raise ImportError("No wandb / wandb がインストールされていないようです")

                    wandb_tracker.log({f"sample_{i}": wandb.Image(image)})
                except:  # wandb 無効時
                    pass

    # clear pipeline and cache to reduce vram usage
    del pipeline
    torch.cuda.empty_cache()

    torch.set_rng_state(rng_state)
    if cuda_rng_state is not None:
        torch.cuda.set_rng_state(cuda_rng_state)
    vae.to(org_vae_device)


# https://www.crosslabs.org//blog/diffusion-with-offset-noise
def apply_noise_offset(noise, noise_offset):
    if noise_offset is None or noise_offset < 0.0000001:
        return noise
    noise = noise + noise_offset * torch.randn((noise.shape[0], noise.shape[1], 1, 1), device=noise.device)
    return noise


if TYPE_CHECKING:
    from toolkit.stable_diffusion_model import PromptEmbeds


def concat_prompt_embeddings(
        unconditional: 'PromptEmbeds',
        conditional: 'PromptEmbeds',
        n_imgs: int,
):
    from toolkit.stable_diffusion_model import PromptEmbeds
    text_embeds = torch.cat(
        [unconditional.text_embeds, conditional.text_embeds]
    ).repeat_interleave(n_imgs, dim=0)
    pooled_embeds = None
    if unconditional.pooled_embeds is not None and conditional.pooled_embeds is not None:
        pooled_embeds = torch.cat(
            [unconditional.pooled_embeds, conditional.pooled_embeds]
        ).repeat_interleave(n_imgs, dim=0)
    return PromptEmbeds([text_embeds, pooled_embeds])


def addnet_hash_safetensors(b):
    """New model hash used by sd-webui-additional-networks for .safetensors format files"""
    hash_sha256 = hashlib.sha256()
    blksize = 1024 * 1024

    b.seek(0)
    header = b.read(8)
    n = int.from_bytes(header, "little")

    offset = n + 8
    b.seek(offset)
    for chunk in iter(lambda: b.read(blksize), b""):
        hash_sha256.update(chunk)

    return hash_sha256.hexdigest()


def addnet_hash_legacy(b):
    """Old model hash used by sd-webui-additional-networks for .safetensors format files"""
    m = hashlib.sha256()

    b.seek(0x100000)
    m.update(b.read(0x10000))
    return m.hexdigest()[0:8]


if TYPE_CHECKING:
    from transformers import CLIPTextModel, CLIPTokenizer, CLIPTextModelWithProjection


def text_tokenize(
        tokenizer: 'CLIPTokenizer',
        prompts: list[str],
        truncate: bool = True,
        max_length: int = None,
        max_length_multiplier: int = 4,
):
    # allow fo up to 4x the max length for long prompts
    if max_length is None:
        if truncate:
            max_length = tokenizer.model_max_length
        else:
            # allow up to 4x the max length for long prompts
            max_length = tokenizer.model_max_length * max_length_multiplier

    input_ids = tokenizer(
        prompts,
        padding='max_length',
        max_length=max_length,
        truncation=True,
        return_tensors="pt",
    ).input_ids

    if truncate or max_length == tokenizer.model_max_length:
        return input_ids
    else:
        # remove additional padding
        num_chunks = input_ids.shape[1] // tokenizer.model_max_length
        chunks = torch.chunk(input_ids, chunks=num_chunks, dim=1)

        # New list to store non-redundant chunks
        non_redundant_chunks = []

        for chunk in chunks:
            if not chunk.eq(chunk[0, 0]).all():  # Check if all elements in the chunk are the same as the first element
                non_redundant_chunks.append(chunk)

        input_ids = torch.cat(non_redundant_chunks, dim=1)
        return input_ids


# https://github.com/huggingface/diffusers/blob/78922ed7c7e66c20aa95159c7b7a6057ba7d590d/src/diffusers/pipelines/stable_diffusion_xl/pipeline_stable_diffusion_xl.py#L334-L348
def text_encode_xl(
        text_encoder: Union['CLIPTextModel', 'CLIPTextModelWithProjection'],
        tokens: torch.FloatTensor,
        num_images_per_prompt: int = 1,
        max_length: int = 77,  # not sure what default to put here, always pass one?
        truncate: bool = True,
):
    if truncate:
        # normal short prompt 77 tokens max
        prompt_embeds = text_encoder(
            tokens.to(text_encoder.device), output_hidden_states=True
        )
        pooled_prompt_embeds = prompt_embeds[0]
        prompt_embeds = prompt_embeds.hidden_states[-2]  # always penultimate layer
    else:
        # handle long prompts
        prompt_embeds_list = []
        tokens = tokens.to(text_encoder.device)
        pooled_prompt_embeds = None
        for i in range(0, tokens.shape[-1], max_length):
            # todo run it through the in a single batch
            section_tokens = tokens[:, i: i + max_length]
            embeds = text_encoder(section_tokens, output_hidden_states=True)
            pooled_prompt_embed = embeds[0]
            if pooled_prompt_embeds is None:
                # we only want the first ( I think??)
                pooled_prompt_embeds = pooled_prompt_embed
            prompt_embed = embeds.hidden_states[-2]  # always penultimate layer
            prompt_embeds_list.append(prompt_embed)

        prompt_embeds = torch.cat(prompt_embeds_list, dim=1)

    bs_embed, seq_len, _ = prompt_embeds.shape
    prompt_embeds = prompt_embeds.repeat(1, num_images_per_prompt, 1)
    prompt_embeds = prompt_embeds.view(bs_embed * num_images_per_prompt, seq_len, -1)

    return prompt_embeds, pooled_prompt_embeds


def encode_prompts_xl(
        tokenizers: list['CLIPTokenizer'],
        text_encoders: list[Union['CLIPTextModel', 'CLIPTextModelWithProjection']],
        prompts: list[str],
        prompts2: Union[list[str], None],
        num_images_per_prompt: int = 1,
        use_text_encoder_1: bool = True,  # sdxl
        use_text_encoder_2: bool = True,  # sdxl
        truncate: bool = True,
        max_length=None,
        dropout_prob=0.0,
) -> tuple[torch.FloatTensor, torch.FloatTensor]:
    # text_encoder and text_encoder_2's penuultimate layer's output
    text_embeds_list = []
    pooled_text_embeds = None  # always text_encoder_2's pool
    if prompts2 is None:
        prompts2 = prompts

    for idx, (tokenizer, text_encoder) in enumerate(zip(tokenizers, text_encoders)):
        # todo, we are using a blank string to ignore that encoder for now.
        # find a better way to do this (zeroing?, removing it from the unet?)
        prompt_list_to_use = prompts if idx == 0 else prompts2
        if idx == 0 and not use_text_encoder_1:
            prompt_list_to_use = ["" for _ in prompts]
        if idx == 1 and not use_text_encoder_2:
            prompt_list_to_use = ["" for _ in prompts]

        if dropout_prob > 0.0:
            # randomly drop out prompts
            prompt_list_to_use = [
                prompt if torch.rand(1).item() > dropout_prob else "" for prompt in prompt_list_to_use
            ]

        text_tokens_input_ids = text_tokenize(tokenizer, prompt_list_to_use, truncate=truncate, max_length=max_length)
        # set the max length for the next one
        if idx == 0:
            max_length = text_tokens_input_ids.shape[-1]

        text_embeds, pooled_text_embeds = text_encode_xl(
            text_encoder, text_tokens_input_ids, num_images_per_prompt, max_length=tokenizer.model_max_length,
            truncate=truncate
        )

        text_embeds_list.append(text_embeds)

    bs_embed = pooled_text_embeds.shape[0]
    pooled_text_embeds = pooled_text_embeds.repeat(1, num_images_per_prompt).view(
        bs_embed * num_images_per_prompt, -1
    )

    return torch.concat(text_embeds_list, dim=-1), pooled_text_embeds


# ref for long prompts https://github.com/huggingface/diffusers/issues/2136
def text_encode(text_encoder: 'CLIPTextModel', tokens, truncate: bool = True, max_length=None):
    if max_length is None and not truncate:
        raise ValueError("max_length must be set if truncate is True")
    try:
        tokens = tokens.to(text_encoder.device)
    except Exception as e:
        print(e)
        print("tokens.device", tokens.device)
        print("text_encoder.device", text_encoder.device)
        raise e

    if truncate:
        return text_encoder(tokens)[0]
    else:
        # handle long prompts
        prompt_embeds_list = []
        for i in range(0, tokens.shape[-1], max_length):
            prompt_embeds = text_encoder(tokens[:, i: i + max_length])[0]
            prompt_embeds_list.append(prompt_embeds)

        return torch.cat(prompt_embeds_list, dim=1)


def encode_prompts(
        tokenizer: 'CLIPTokenizer',
        text_encoder: 'CLIPTextModel',
        prompts: list[str],
        truncate: bool = True,
        max_length=None,
        dropout_prob=0.0,
):
    if max_length is None:
        max_length = tokenizer.model_max_length

    if dropout_prob > 0.0:
        # randomly drop out prompts
        prompts = [
            prompt if torch.rand(1).item() > dropout_prob else "" for prompt in prompts
        ]

    text_tokens = text_tokenize(tokenizer, prompts, truncate=truncate, max_length=max_length)
    text_embeddings = text_encode(text_encoder, text_tokens, truncate=truncate, max_length=max_length)

    return text_embeddings


# for XL
def get_add_time_ids(
        height: int,
        width: int,
        dynamic_crops: bool = False,
        dtype: torch.dtype = torch.float32,
):
    if dynamic_crops:
        # random float scale between 1 and 3
        random_scale = torch.rand(1).item() * 2 + 1
        original_size = (int(height * random_scale), int(width * random_scale))
        # random position
        crops_coords_top_left = (
            torch.randint(0, original_size[0] - height, (1,)).item(),
            torch.randint(0, original_size[1] - width, (1,)).item(),
        )
        target_size = (height, width)
    else:
        original_size = (height, width)
        crops_coords_top_left = (0, 0)
        target_size = (height, width)

    # this is expected as 6
    add_time_ids = list(original_size + crops_coords_top_left + target_size)

    # this is expected as 2816
    passed_add_embed_dim = (
            UNET_ATTENTION_TIME_EMBED_DIM * len(add_time_ids)  # 256 * 6
            + TEXT_ENCODER_2_PROJECTION_DIM  # + 1280
    )
    if passed_add_embed_dim != UNET_PROJECTION_CLASS_EMBEDDING_INPUT_DIM:
        raise ValueError(
            f"Model expects an added time embedding vector of length {UNET_PROJECTION_CLASS_EMBEDDING_INPUT_DIM}, but a vector of {passed_add_embed_dim} was created. The model has an incorrect config. Please check `unet.config.time_embedding_type` and `text_encoder_2.config.projection_dim`."
        )

    add_time_ids = torch.tensor([add_time_ids], dtype=dtype)
    return add_time_ids


def concat_embeddings(
        unconditional: torch.FloatTensor,
        conditional: torch.FloatTensor,
        n_imgs: int,
):
    return torch.cat([unconditional, conditional]).repeat_interleave(n_imgs, dim=0)


def add_all_snr_to_noise_scheduler(noise_scheduler, device):
    if hasattr(noise_scheduler, "all_snr"):
        return
    # compute it
    with torch.no_grad():
        alphas_cumprod = noise_scheduler.alphas_cumprod
        sqrt_alphas_cumprod = torch.sqrt(alphas_cumprod)
        sqrt_one_minus_alphas_cumprod = torch.sqrt(1.0 - alphas_cumprod)
        alpha = sqrt_alphas_cumprod
        sigma = sqrt_one_minus_alphas_cumprod
        all_snr = (alpha / sigma) ** 2
        all_snr.requires_grad = False
    noise_scheduler.all_snr = all_snr.to(device)


def get_all_snr(noise_scheduler, device):
    if hasattr(noise_scheduler, "all_snr"):
        return noise_scheduler.all_snr.to(device)
    # compute it
    with torch.no_grad():
        alphas_cumprod = noise_scheduler.alphas_cumprod
        sqrt_alphas_cumprod = torch.sqrt(alphas_cumprod)
        sqrt_one_minus_alphas_cumprod = torch.sqrt(1.0 - alphas_cumprod)
        alpha = sqrt_alphas_cumprod
        sigma = sqrt_one_minus_alphas_cumprod
        all_snr = (alpha / sigma) ** 2
        all_snr.requires_grad = False
    return all_snr.to(device)

class LearnableSNRGamma:
    """
    This is a trainer for learnable snr gamma
    It will adapt to the dataset and attempt to adjust the snr multiplier to balance the loss over the timesteps
    """
    def __init__(self, noise_scheduler: Union['DDPMScheduler'], device='cuda'):
        self.device = device
        self.noise_scheduler: Union['DDPMScheduler'] = noise_scheduler
        self.offset_1 = torch.nn.Parameter(torch.tensor(0.0, dtype=torch.float32, device=device))
        self.offset_2 = torch.nn.Parameter(torch.tensor(0.777, dtype=torch.float32, device=device))
        self.scale = torch.nn.Parameter(torch.tensor(4.14, dtype=torch.float32, device=device))
        self.gamma = torch.nn.Parameter(torch.tensor(2.03, dtype=torch.float32, device=device))
        self.optimizer = torch.optim.AdamW([self.offset_1, self.offset_2, self.gamma, self.scale], lr=0.01)
        self.buffer = []
        self.max_buffer_size = 20

    def forward(self, loss, timesteps):
        # do a our train loop for lsnr here and return our values detached
        loss = loss.detach()
        with torch.no_grad():
            loss_chunks = torch.chunk(loss, loss.shape[0], dim=0)
            for loss_chunk in loss_chunks:
                self.buffer.append(loss_chunk.mean().detach())
                if len(self.buffer) > self.max_buffer_size:
                    self.buffer.pop(0)
            all_snr = get_all_snr(self.noise_scheduler, loss.device)
            snr: torch.Tensor = torch.stack([all_snr[t] for t in timesteps]).detach().float().to(loss.device)
        base_snrs = snr.clone().detach()
        snr.requires_grad = True
        snr = (snr + self.offset_1) * self.scale + self.offset_2

        gamma_over_snr = torch.div(torch.ones_like(snr) * self.gamma, snr)
        snr_weight = torch.abs(gamma_over_snr).float().to(loss.device)  # directly using gamma over snr
        snr_adjusted_loss = loss * snr_weight
        with torch.no_grad():
            target = torch.mean(torch.stack(self.buffer)).detach()

        # local_loss = torch.mean(torch.abs(snr_adjusted_loss - target))
        squared_differences = (snr_adjusted_loss - target) ** 2
        local_loss = torch.mean(squared_differences)
        local_loss.backward()
        self.optimizer.step()
        self.optimizer.zero_grad()

        return base_snrs, self.gamma.detach(), self.offset_1.detach(), self.offset_2.detach(), self.scale.detach()


def apply_learnable_snr_gos(
        loss,
        timesteps,
        learnable_snr_trainer: LearnableSNRGamma
):

    snr, gamma, offset_1, offset_2, scale = learnable_snr_trainer.forward(loss, timesteps)

    snr = (snr + offset_1) * scale + offset_2

    gamma_over_snr = torch.div(torch.ones_like(snr) * gamma, snr)
    snr_weight = torch.abs(gamma_over_snr).float().to(loss.device)  # directly using gamma over snr
    snr_adjusted_loss = loss * snr_weight

    return snr_adjusted_loss


def apply_snr_weight(
        loss,
        timesteps,
        noise_scheduler: Union['DDPMScheduler'],
        gamma,
        fixed=False,
):
    # will get it from noise scheduler if exist or will calculate it if not
    all_snr = get_all_snr(noise_scheduler, loss.device)
    # step_indices = []
    # for t in timesteps:
    #     for i, st in enumerate(noise_scheduler.timesteps):
    #         if st == t:
    #             step_indices.append(i)
    #             break
    # this breaks on some schedulers
    # step_indices = [(noise_scheduler.timesteps == t).nonzero().item() for t in timesteps]

    offset = 0
    if noise_scheduler.timesteps[0] == 1000:
        offset = 1
    snr = torch.stack([all_snr[(t - offset).int()] for t in timesteps])
    gamma_over_snr = torch.div(torch.ones_like(snr) * gamma, snr)
    if fixed:
        snr_weight = gamma_over_snr.float().to(loss.device)  # directly using gamma over snr
    else:
        snr_weight = torch.minimum(gamma_over_snr, torch.ones_like(gamma_over_snr)).float().to(loss.device)
    snr_adjusted_loss = loss * snr_weight

    return snr_adjusted_loss
