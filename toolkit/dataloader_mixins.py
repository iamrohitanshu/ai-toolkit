import base64
import hashlib
import json
import math
import os
import random
from collections import OrderedDict
from typing import TYPE_CHECKING, List, Dict, Union

import cv2
import numpy as np
import torch
from safetensors.torch import load_file, save_file
from tqdm import tqdm
from transformers import CLIPImageProcessor

from toolkit.basic import flush, value_map
from toolkit.buckets import get_bucket_for_image_size, get_resolution
from toolkit.metadata import get_meta_for_safetensors
from toolkit.prompt_utils import inject_trigger_into_prompt
from torchvision import transforms
from PIL import Image, ImageFilter, ImageOps
from PIL.ImageOps import exif_transpose
import albumentations as A

from toolkit.train_tools import get_torch_dtype

if TYPE_CHECKING:
    from toolkit.data_loader import AiToolkitDataset
    from toolkit.data_transfer_object.data_loader import FileItemDTO
    from toolkit.stable_diffusion_model import StableDiffusion

# def get_associated_caption_from_img_path(img_path):
# https://demo.albumentations.ai/
class Augments:
    def __init__(self, **kwargs):
        self.method_name = kwargs.get('method', None)
        self.params = kwargs.get('params', {})

        # convert kwargs enums for cv2
        for key, value in self.params.items():
            if isinstance(value, str):
                # split the string
                split_string = value.split('.')
                if len(split_string) == 2 and split_string[0] == 'cv2':
                    if hasattr(cv2, split_string[1]):
                        self.params[key] = getattr(cv2, split_string[1].upper())
                    else:
                        raise ValueError(f"invalid cv2 enum: {split_string[1]}")


transforms_dict = {
    'ColorJitter': transforms.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0.03),
    'RandomEqualize': transforms.RandomEqualize(p=0.2),
}

caption_ext_list = ['txt', 'json', 'caption']


def standardize_images(images):
    """
    Standardize the given batch of images using the specified mean and std.
    Expects values of 0 - 1

    Args:
    images (torch.Tensor): A batch of images in the shape of (N, C, H, W),
                           where N is the number of images, C is the number of channels,
                           H is the height, and W is the width.

    Returns:
    torch.Tensor: Standardized images.
    """
    mean = [0.48145466, 0.4578275, 0.40821073]
    std = [0.26862954, 0.26130258, 0.27577711]

    # Define the normalization transform
    normalize = transforms.Normalize(mean=mean, std=std)

    # Apply normalization to each image in the batch
    standardized_images = torch.stack([normalize(img) for img in images])

    return standardized_images

def clean_caption(caption):
    # remove any newlines
    caption = caption.replace('\n', ', ')
    # remove new lines for all operating systems
    caption = caption.replace('\r', ', ')
    caption_split = caption.split(',')
    # remove empty strings
    caption_split = [p.strip() for p in caption_split if p.strip()]
    # join back together
    caption = ', '.join(caption_split)
    return caption


class CaptionMixin:
    def get_caption_item(self: 'AiToolkitDataset', index):
        if not hasattr(self, 'caption_type'):
            raise Exception('caption_type not found on class instance')
        if not hasattr(self, 'file_list'):
            raise Exception('file_list not found on class instance')
        img_path_or_tuple = self.file_list[index]
        if isinstance(img_path_or_tuple, tuple):
            img_path = img_path_or_tuple[0] if isinstance(img_path_or_tuple[0], str) else img_path_or_tuple[0].path
            # check if either has a prompt file
            path_no_ext = os.path.splitext(img_path)[0]
            prompt_path = None
            for ext in caption_ext_list:
                prompt_path = path_no_ext + '.' + ext
                if os.path.exists(prompt_path):
                    break
        else:
            img_path = img_path_or_tuple if isinstance(img_path_or_tuple, str) else img_path_or_tuple.path
            # see if prompt file exists
            path_no_ext = os.path.splitext(img_path)[0]
            prompt_path = None
            for ext in caption_ext_list:
                prompt_path = path_no_ext + '.' + ext
                if os.path.exists(prompt_path):
                    break

        if os.path.exists(prompt_path):
            with open(prompt_path, 'r', encoding='utf-8') as f:
                prompt = f.read()
                # check if is json
                if prompt_path.endswith('.json'):
                    prompt = json.loads(prompt)
                    if 'caption' in prompt:
                        prompt = prompt['caption']

                prompt = clean_caption(prompt)
        else:
            prompt = ''
            # get default_prompt if it exists on the class instance
            if hasattr(self, 'default_prompt'):
                prompt = self.default_prompt
            if hasattr(self, 'default_caption'):
                prompt = self.default_caption
        return prompt


if TYPE_CHECKING:
    from toolkit.config_modules import DatasetConfig
    from toolkit.data_transfer_object.data_loader import FileItemDTO


class Bucket:
    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self.file_list_idx: List[int] = []


class BucketsMixin:
    def __init__(self):
        self.buckets: Dict[str, Bucket] = {}
        self.batch_indices: List[List[int]] = []

    def build_batch_indices(self: 'AiToolkitDataset'):
        self.batch_indices = []
        for key, bucket in self.buckets.items():
            for start_idx in range(0, len(bucket.file_list_idx), self.batch_size):
                end_idx = min(start_idx + self.batch_size, len(bucket.file_list_idx))
                batch = bucket.file_list_idx[start_idx:end_idx]
                self.batch_indices.append(batch)

    def shuffle_buckets(self: 'AiToolkitDataset'):
        for key, bucket in self.buckets.items():
            random.shuffle(bucket.file_list_idx)

    def setup_buckets(self: 'AiToolkitDataset', quiet=False):
        if not hasattr(self, 'file_list'):
            raise Exception(f'file_list not found on class instance {self.__class__.__name__}')
        if not hasattr(self, 'dataset_config'):
            raise Exception(f'dataset_config not found on class instance {self.__class__.__name__}')

        if self.epoch_num > 0 and self.dataset_config.poi is None:
            # no need to rebuild buckets for now
            # todo handle random cropping for buckets
            return
        self.buckets = {}  # clear it

        config: 'DatasetConfig' = self.dataset_config
        resolution = config.resolution
        bucket_tolerance = config.bucket_tolerance
        file_list: List['FileItemDTO'] = self.file_list

        # for file_item in enumerate(file_list):
        for idx, file_item in enumerate(file_list):
            file_item: 'FileItemDTO' = file_item
            width = int(file_item.width * file_item.dataset_config.scale)
            height = int(file_item.height * file_item.dataset_config.scale)

            did_process_poi = False
            if file_item.has_point_of_interest:
                # Attempt to process the poi if we can. It wont process if the image is smaller than the resolution
                did_process_poi = file_item.setup_poi_bucket()
            if not did_process_poi:
                bucket_resolution = get_bucket_for_image_size(
                    width, height,
                    resolution=resolution,
                    divisibility=bucket_tolerance
                )

                # Calculate scale factors for width and height
                width_scale_factor = bucket_resolution["width"] / width
                height_scale_factor = bucket_resolution["height"] / height

                # Use the maximum of the scale factors to ensure both dimensions are scaled above the bucket resolution
                max_scale_factor = max(width_scale_factor, height_scale_factor)

                # round up
                file_item.scale_to_width = int(math.ceil(width * max_scale_factor))
                file_item.scale_to_height = int(math.ceil(height * max_scale_factor))

                file_item.crop_height = bucket_resolution["height"]
                file_item.crop_width = bucket_resolution["width"]

                new_width = bucket_resolution["width"]
                new_height = bucket_resolution["height"]

                if self.dataset_config.random_crop:
                    # random crop
                    crop_x = random.randint(0, file_item.scale_to_width - new_width)
                    crop_y = random.randint(0, file_item.scale_to_height - new_height)
                    file_item.crop_x = crop_x
                    file_item.crop_y = crop_y
                else:
                    # do central crop
                    file_item.crop_x = int((file_item.scale_to_width - new_width) / 2)
                    file_item.crop_y = int((file_item.scale_to_height - new_height) / 2)

                if file_item.crop_y < 0 or file_item.crop_x < 0:
                    print('debug')

            # check if bucket exists, if not, create it
            bucket_key = f'{file_item.crop_width}x{file_item.crop_height}'
            if bucket_key not in self.buckets:
                self.buckets[bucket_key] = Bucket(file_item.crop_width, file_item.crop_height)
            self.buckets[bucket_key].file_list_idx.append(idx)

        # print the buckets
        self.shuffle_buckets()
        self.build_batch_indices()
        if not quiet:
            print(f'Bucket sizes for {self.dataset_path}:')
            for key, bucket in self.buckets.items():
                print(f'{key}: {len(bucket.file_list_idx)} files')
            print(f'{len(self.buckets)} buckets made')


class CaptionProcessingDTOMixin:
    def __init__(self: 'FileItemDTO', *args, **kwargs):
        if hasattr(super(), '__init__'):
            super().__init__(*args, **kwargs)
            self.raw_caption: str = None
            self.raw_caption_short: str = None

    # todo allow for loading from sd-scripts style dict
    def load_caption(self: 'FileItemDTO', caption_dict: Union[dict, None]):
        if self.raw_caption is not None:
            # we already loaded it
            pass
        elif caption_dict is not None and self.path in caption_dict and "caption" in caption_dict[self.path]:
            self.raw_caption = caption_dict[self.path]["caption"]
            if 'caption_short' in caption_dict[self.path]:
                self.raw_caption_short = caption_dict[self.path]["caption_short"]
        else:
            # see if prompt file exists
            path_no_ext = os.path.splitext(self.path)[0]
            prompt_ext = self.dataset_config.caption_ext
            prompt_path = f"{path_no_ext}.{prompt_ext}"
            short_caption = None

            if os.path.exists(prompt_path):
                with open(prompt_path, 'r', encoding='utf-8') as f:
                    prompt = f.read()
                    short_caption = None
                    if prompt_path.endswith('.json'):
                        # replace any line endings with commas for \n \r \r\n
                        prompt = prompt.replace('\r\n', ' ')
                        prompt = prompt.replace('\n', ' ')
                        prompt = prompt.replace('\r', ' ')

                        prompt = json.loads(prompt)
                        if 'caption' in prompt:
                            prompt = prompt['caption']
                        if 'caption_short' in prompt:
                            short_caption = prompt['caption_short']
                    prompt = clean_caption(prompt)
                    if short_caption is not None:
                        short_caption = clean_caption(short_caption)
            else:
                prompt = ''
                if self.dataset_config.default_caption is not None:
                    prompt = self.dataset_config.default_caption

            if short_caption is None:
                short_caption = self.dataset_config.default_caption
            self.raw_caption = prompt
            self.raw_caption_short = short_caption

    def get_caption(
            self: 'FileItemDTO',
            trigger=None,
            to_replace_list=None,
            add_if_not_present=False,
            short_caption=False
    ):
        if short_caption:
            raw_caption = self.raw_caption_short
        else:
            raw_caption = self.raw_caption
        if raw_caption is None:
            raw_caption = ''
        # handle dropout
        if self.dataset_config.caption_dropout_rate > 0 and not short_caption:
            # get a random float form 0 to 1
            rand = random.random()
            if rand < self.dataset_config.caption_dropout_rate:
                # drop the caption
                return ''

        # get tokens
        token_list = raw_caption.split(',')
        # trim whitespace
        token_list = [x.strip() for x in token_list]
        # remove empty strings
        token_list = [x for x in token_list if x]

        if self.dataset_config.shuffle_tokens:
            random.shuffle(token_list)

        # handle token dropout
        if self.dataset_config.token_dropout_rate > 0 and not short_caption:
            new_token_list = []
            for token in token_list:
                # get a random float form 0 to 1
                rand = random.random()
                if rand > self.dataset_config.token_dropout_rate:
                    # keep the token
                    new_token_list.append(token)
            token_list = new_token_list

        # join back together
        caption = ', '.join(token_list)
        caption = inject_trigger_into_prompt(caption, trigger, to_replace_list, add_if_not_present)

        if self.dataset_config.random_triggers and len(self.dataset_config.random_triggers) > 0:
            num_triggers = self.dataset_config.random_triggers_max
            if num_triggers > 1:
                num_triggers = random.randint(0, num_triggers)

            if num_triggers > 0:
                # add random triggers
                for i in range(num_triggers):
                    caption = caption + ', ' + random.choice(self.dataset_config.random_triggers)

        if self.dataset_config.shuffle_tokens:
            # shuffle again
            token_list = caption.split(',')
            # trim whitespace
            token_list = [x.strip() for x in token_list]
            # remove empty strings
            token_list = [x for x in token_list if x]
            random.shuffle(token_list)
            caption = ', '.join(token_list)

        return caption


class ImageProcessingDTOMixin:
    def load_and_process_image(
            self: 'FileItemDTO',
            transform: Union[None, transforms.Compose],
            only_load_latents=False
    ):
        # if we are caching latents, just do that
        if self.is_latent_cached:
            self.get_latent()
            if self.has_control_image:
                self.load_control_image()
            if self.has_clip_image:
                self.load_clip_image()
            if self.has_mask_image:
                self.load_mask_image()
            if self.has_unconditional:
                self.load_unconditional_image()
            return
        try:
            img = Image.open(self.path)
            img = exif_transpose(img)
        except Exception as e:
            print(f"Error: {e}")
            print(f"Error loading image: {self.path}")

        if self.use_alpha_as_mask:
            # we do this to make sure it does not replace the alpha with another color
            # we want the image just without the alpha channel
            np_img = np.array(img)
            # strip off alpha
            np_img = np_img[:, :, :3]
            img = Image.fromarray(np_img)

        img = img.convert('RGB')
        w, h = img.size
        if w > h and self.scale_to_width < self.scale_to_height:
            # throw error, they should match
            raise ValueError(
                f"unexpected values: w={w}, h={h}, file_item.scale_to_width={self.scale_to_width}, file_item.scale_to_height={self.scale_to_height}, file_item.path={self.path}")
        elif h > w and self.scale_to_height < self.scale_to_width:
            # throw error, they should match
            raise ValueError(
                f"unexpected values: w={w}, h={h}, file_item.scale_to_width={self.scale_to_width}, file_item.scale_to_height={self.scale_to_height}, file_item.path={self.path}")

        if self.flip_x:
            # do a flip
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
        if self.flip_y:
            # do a flip
            img = img.transpose(Image.FLIP_TOP_BOTTOM)

        if self.dataset_config.buckets:
            # scale and crop based on file item
            img = img.resize((self.scale_to_width, self.scale_to_height), Image.BICUBIC)
            # crop to x_crop, y_crop, x_crop + crop_width, y_crop + crop_height
            if img.width < self.crop_x + self.crop_width or img.height < self.crop_y + self.crop_height:
                # todo look into this. This still happens sometimes
                print('size mismatch')
            img = img.crop((
                self.crop_x,
                self.crop_y,
                self.crop_x + self.crop_width,
                self.crop_y + self.crop_height
            ))

            # img = transforms.CenterCrop((self.crop_height, self.crop_width))(img)
        else:
            # Downscale the source image first
            # TODO this is nto right
            img = img.resize(
                (int(img.size[0] * self.dataset_config.scale), int(img.size[1] * self.dataset_config.scale)),
                Image.BICUBIC)
            min_img_size = min(img.size)
            if self.dataset_config.random_crop:
                if self.dataset_config.random_scale and min_img_size > self.dataset_config.resolution:
                    if min_img_size < self.dataset_config.resolution:
                        print(
                            f"Unexpected values: min_img_size={min_img_size}, self.resolution={self.dataset_config.resolution}, image file={self.path}")
                        scale_size = self.dataset_config.resolution
                    else:
                        scale_size = random.randint(self.dataset_config.resolution, int(min_img_size))
                    scaler = scale_size / min_img_size
                    scale_width = int((img.width + 5) * scaler)
                    scale_height = int((img.height + 5) * scaler)
                    img = img.resize((scale_width, scale_height), Image.BICUBIC)
                img = transforms.RandomCrop(self.dataset_config.resolution)(img)
            else:
                img = transforms.CenterCrop(min_img_size)(img)
                img = img.resize((self.dataset_config.resolution, self.dataset_config.resolution), Image.BICUBIC)

        if self.augments is not None and len(self.augments) > 0:
            # do augmentations
            for augment in self.augments:
                if augment in transforms_dict:
                    img = transforms_dict[augment](img)

        if self.has_augmentations:
            # augmentations handles transforms
            img = self.augment_image(img, transform=transform)
        elif transform:
            img = transform(img)

        self.tensor = img
        if not only_load_latents:
            if self.has_control_image:
                self.load_control_image()
            if self.has_clip_image:
                self.load_clip_image()
            if self.has_mask_image:
                self.load_mask_image()
            if self.has_unconditional:
                self.load_unconditional_image()


class ControlFileItemDTOMixin:
    def __init__(self: 'FileItemDTO', *args, **kwargs):
        if hasattr(super(), '__init__'):
            super().__init__(*args, **kwargs)
        self.has_control_image = False
        self.control_path: Union[str, None] = None
        self.control_tensor: Union[torch.Tensor, None] = None
        dataset_config: 'DatasetConfig' = kwargs.get('dataset_config', None)
        self.full_size_control_images = False
        if dataset_config.control_path is not None:
            # find the control image path
            control_path = dataset_config.control_path
            self.full_size_control_images = dataset_config.full_size_control_images
            # we are using control images
            img_path = kwargs.get('path', None)
            img_ext_list = ['.jpg', '.jpeg', '.png', '.webp']
            file_name_no_ext = os.path.splitext(os.path.basename(img_path))[0]
            for ext in img_ext_list:
                if os.path.exists(os.path.join(control_path, file_name_no_ext + ext)):
                    self.control_path = os.path.join(control_path, file_name_no_ext + ext)
                    self.has_control_image = True
                    break

    def load_control_image(self: 'FileItemDTO'):
        try:
            img = Image.open(self.control_path).convert('RGB')
            img = exif_transpose(img)
        except Exception as e:
            print(f"Error: {e}")
            print(f"Error loading image: {self.control_path}")

        if self.full_size_control_images:
            # we just scale them to 512x512:
            w, h = img.size
            img = img.resize((512, 512), Image.BICUBIC)

        else:
            w, h = img.size
            if w > h and self.scale_to_width < self.scale_to_height:
                # throw error, they should match
                raise ValueError(
                    f"unexpected values: w={w}, h={h}, file_item.scale_to_width={self.scale_to_width}, file_item.scale_to_height={self.scale_to_height}, file_item.path={self.path}")
            elif h > w and self.scale_to_height < self.scale_to_width:
                # throw error, they should match
                raise ValueError(
                    f"unexpected values: w={w}, h={h}, file_item.scale_to_width={self.scale_to_width}, file_item.scale_to_height={self.scale_to_height}, file_item.path={self.path}")

            if self.flip_x:
                # do a flip
                img = img.transpose(Image.FLIP_LEFT_RIGHT)
            if self.flip_y:
                # do a flip
                img = img.transpose(Image.FLIP_TOP_BOTTOM)

            if self.dataset_config.buckets:
                # scale and crop based on file item
                img = img.resize((self.scale_to_width, self.scale_to_height), Image.BICUBIC)
                # img = transforms.CenterCrop((self.crop_height, self.crop_width))(img)
                # crop
                img = img.crop((
                    self.crop_x,
                    self.crop_y,
                    self.crop_x + self.crop_width,
                    self.crop_y + self.crop_height
                ))
            else:
                raise Exception("Control images not supported for non-bucket datasets")
        transform = transforms.Compose([
            transforms.ToTensor(),
        ])
        if self.aug_replay_spatial_transforms:
            self.control_tensor = self.augment_spatial_control(img, transform=transform)
        else:
            self.control_tensor = transform(img)

    def cleanup_control(self: 'FileItemDTO'):
        self.control_tensor = None


class ClipImageFileItemDTOMixin:
    def __init__(self: 'FileItemDTO', *args, **kwargs):
        if hasattr(super(), '__init__'):
            super().__init__(*args, **kwargs)
        self.has_clip_image = False
        self.clip_image_path: Union[str, None] = None
        self.clip_image_tensor: Union[torch.Tensor, None] = None
        self.has_clip_augmentations = False
        self.clip_image_aug_transform: Union[None, A.Compose] = None
        self.clip_image_processor: Union[None, CLIPImageProcessor] = None
        dataset_config: 'DatasetConfig' = kwargs.get('dataset_config', None)
        if dataset_config.clip_image_path is not None:
            # copy the clip image processor so the dataloader can do it
            sd = kwargs.get('sd', None)
            if hasattr(sd.adapter, 'clip_image_processor'):
                self.clip_image_processor = sd.adapter.clip_image_processor
            # find the control image path
            clip_image_path = dataset_config.clip_image_path
            # we are using control images
            img_path = kwargs.get('path', None)
            img_ext_list = ['.jpg', '.jpeg', '.png', '.webp']
            file_name_no_ext = os.path.splitext(os.path.basename(img_path))[0]
            for ext in img_ext_list:
                if os.path.exists(os.path.join(clip_image_path, file_name_no_ext + ext)):
                    self.clip_image_path = os.path.join(clip_image_path, file_name_no_ext + ext)
                    self.has_clip_image = True
                    break

            self.build_clip_imag_augmentation_transform()

    def build_clip_imag_augmentation_transform(self: 'FileItemDTO'):
        if self.dataset_config.clip_image_augmentations is not None and len(self.dataset_config.clip_image_augmentations) > 0:
            self.has_clip_augmentations = True
            augmentations = [Augments(**aug) for aug in self.dataset_config.clip_image_augmentations]

            if self.dataset_config.clip_image_shuffle_augmentations:
                random.shuffle(augmentations)

            augmentation_list = []
            for aug in augmentations:
                # make sure method name is valid
                assert hasattr(A, aug.method_name), f"invalid augmentation method: {aug.method_name}"
                # get the method
                method = getattr(A, aug.method_name)
                # add the method to the list
                augmentation_list.append(method(**aug.params))

            self.clip_image_aug_transform = A.Compose(augmentation_list)

    def augment_clip_image(self: 'FileItemDTO', img: Image, transform: Union[None, transforms.Compose], ):
        if self.dataset_config.clip_image_shuffle_augmentations:
            self.build_clip_imag_augmentation_transform()

        open_cv_image = np.array(img)
        # Convert RGB to BGR
        open_cv_image = open_cv_image[:, :, ::-1].copy()

        # apply augmentations
        augmented = self.clip_image_aug_transform(image=open_cv_image)["image"]

        # convert back to RGB tensor
        augmented = cv2.cvtColor(augmented, cv2.COLOR_BGR2RGB)

        # convert to PIL image
        augmented = Image.fromarray(augmented)

        augmented_tensor = transforms.ToTensor()(augmented) if transform is None else transform(augmented)

        return augmented_tensor

    def load_clip_image(self: 'FileItemDTO'):
        img = Image.open(self.clip_image_path).convert('RGB')
        try:
            img = exif_transpose(img)
        except Exception as e:
            print(f"Error: {e}")
            print(f"Error loading image: {self.clip_image_path}")

        img = img.convert('RGB')

        if self.flip_x:
            # do a flip
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
        if self.flip_y:
            # do a flip
            img = img.transpose(Image.FLIP_TOP_BOTTOM)

        if self.has_clip_augmentations:
            self.clip_image_tensor = self.augment_clip_image(img, transform=None)
        else:
            self.clip_image_tensor = transforms.ToTensor()(img)

        if self.clip_image_processor is not None:
            # run it
            tensors_0_1 = self.clip_image_tensor.to(dtype=torch.float16)
            clip_out = self.clip_image_processor(
                images=tensors_0_1,
                return_tensors="pt",
                do_resize=True,
                do_rescale=False,
            ).pixel_values
            self.clip_image_tensor = clip_out.squeeze(0).clone().detach()

    def cleanup_clip_image(self: 'FileItemDTO'):
        self.clip_image_tensor = None




class AugmentationFileItemDTOMixin:
    def __init__(self: 'FileItemDTO', *args, **kwargs):
        if hasattr(super(), '__init__'):
            super().__init__(*args, **kwargs)
        self.has_augmentations = False
        self.unaugmented_tensor: Union[torch.Tensor, None] = None
        # self.augmentations: Union[None, List[Augments]] = None
        self.dataset_config: 'DatasetConfig' = kwargs.get('dataset_config', None)
        self.aug_transform: Union[None, A.Compose] = None
        self.aug_replay_spatial_transforms = None
        self.build_augmentation_transform()

    def build_augmentation_transform(self: 'FileItemDTO'):
        if self.dataset_config.augmentations is not None and len(self.dataset_config.augmentations) > 0:
            self.has_augmentations = True
            augmentations = [Augments(**aug) for aug in self.dataset_config.augmentations]

            if self.dataset_config.shuffle_augmentations:
                random.shuffle(augmentations)

            augmentation_list = []
            for aug in augmentations:
                # make sure method name is valid
                assert hasattr(A, aug.method_name), f"invalid augmentation method: {aug.method_name}"
                # get the method
                method = getattr(A, aug.method_name)
                # add the method to the list
                augmentation_list.append(method(**aug.params))

            # add additional targets so we can augment the control image
            self.aug_transform = A.ReplayCompose(augmentation_list, additional_targets={'image2': 'image'})

    def augment_image(self: 'FileItemDTO', img: Image, transform: Union[None, transforms.Compose], ):

        # rebuild each time if shuffle
        if self.dataset_config.shuffle_augmentations:
            self.build_augmentation_transform()

        # save the original tensor
        self.unaugmented_tensor = transforms.ToTensor()(img) if transform is None else transform(img)

        open_cv_image = np.array(img)
        # Convert RGB to BGR
        open_cv_image = open_cv_image[:, :, ::-1].copy()

        # apply augmentations
        transformed = self.aug_transform(image=open_cv_image)
        augmented = transformed["image"]

        # save just the spatial transforms for controls and masks
        augmented_params = transformed["replay"]
        spatial_transforms = ['Rotate', 'Flip', 'HorizontalFlip', 'VerticalFlip', 'Resize', 'Crop', 'RandomCrop',
                              'ElasticTransform', 'GridDistortion', 'OpticalDistortion']
        # only store the spatial transforms
        augmented_params['transforms'] = [t for t in augmented_params['transforms'] if t['__class_fullname__'].split('.')[-1] in spatial_transforms]

        self.aug_replay_spatial_transforms = augmented_params

        # convert back to RGB tensor
        augmented = cv2.cvtColor(augmented, cv2.COLOR_BGR2RGB)

        # convert to PIL image
        augmented = Image.fromarray(augmented)

        augmented_tensor = transforms.ToTensor()(augmented) if transform is None else transform(augmented)

        return augmented_tensor

    # augment control images spatially consistent with transforms done to the main image
    def augment_spatial_control(self: 'FileItemDTO', img: Image, transform: Union[None, transforms.Compose] ):
        if self.aug_replay_spatial_transforms is None:
            # no transforms
            return transform(img)

        # save colorspace to convert back to
        colorspace = img.mode

        # convert to rgb
        img = img.convert('RGB')

        open_cv_image = np.array(img)
        # Convert RGB to BGR
        open_cv_image = open_cv_image[:, :, ::-1].copy()

        # Replay transforms
        transformed = A.ReplayCompose.replay(self.aug_replay_spatial_transforms, image=open_cv_image)
        augmented = transformed["image"]

        # convert back to RGB tensor
        augmented = cv2.cvtColor(augmented, cv2.COLOR_BGR2RGB)

        # convert to PIL image
        augmented = Image.fromarray(augmented)

        # convert back to original colorspace
        augmented = augmented.convert(colorspace)

        augmented_tensor = transforms.ToTensor()(augmented) if transform is None else transform(augmented)
        return augmented_tensor

    def cleanup_control(self: 'FileItemDTO'):
        self.unaugmented_tensor = None


class MaskFileItemDTOMixin:
    def __init__(self: 'FileItemDTO', *args, **kwargs):
        if hasattr(super(), '__init__'):
            super().__init__(*args, **kwargs)
        self.has_mask_image = False
        self.mask_path: Union[str, None] = None
        self.mask_tensor: Union[torch.Tensor, None] = None
        self.use_alpha_as_mask: bool = False
        dataset_config: 'DatasetConfig' = kwargs.get('dataset_config', None)
        self.mask_min_value = dataset_config.mask_min_value
        if dataset_config.alpha_mask:
            self.use_alpha_as_mask = True
            self.mask_path = kwargs.get('path', None)
            self.has_mask_image = True
        elif dataset_config.mask_path is not None:
            # find the control image path
            mask_path = dataset_config.mask_path if dataset_config.mask_path is not None else dataset_config.alpha_mask
            # we are using control images
            img_path = kwargs.get('path', None)
            img_ext_list = ['.jpg', '.jpeg', '.png', '.webp']
            file_name_no_ext = os.path.splitext(os.path.basename(img_path))[0]
            for ext in img_ext_list:
                if os.path.exists(os.path.join(mask_path, file_name_no_ext + ext)):
                    self.mask_path = os.path.join(mask_path, file_name_no_ext + ext)
                    self.has_mask_image = True
                    break

    def load_mask_image(self: 'FileItemDTO'):
        try:
            img = Image.open(self.mask_path)
            img = exif_transpose(img)
        except Exception as e:
            print(f"Error: {e}")
            print(f"Error loading image: {self.mask_path}")

        if self.use_alpha_as_mask:
            # pipeline expectws an rgb image so we need to put alpha in all channels
            np_img = np.array(img)
            np_img[:, :, :3] = np_img[:, :, 3:]

            np_img = np_img[:, :, :3]
            img = Image.fromarray(np_img)

        img = img.convert('RGB')
        if self.dataset_config.invert_mask:
            img = ImageOps.invert(img)
        w, h = img.size
        fix_size = False
        if w > h and self.scale_to_width < self.scale_to_height:
            # throw error, they should match
            print(f"unexpected values: w={w}, h={h}, file_item.scale_to_width={self.scale_to_width}, file_item.scale_to_height={self.scale_to_height}, file_item.path={self.path}")
            fix_size = True
        elif h > w and self.scale_to_height < self.scale_to_width:
            # throw error, they should match
            print(f"unexpected values: w={w}, h={h}, file_item.scale_to_width={self.scale_to_width}, file_item.scale_to_height={self.scale_to_height}, file_item.path={self.path}")
            fix_size = True

        if fix_size:
            # swap all the sizes
            self.scale_to_width, self.scale_to_height = self.scale_to_height, self.scale_to_width
            self.crop_width, self.crop_height = self.crop_height, self.crop_width
            self.crop_x, self.crop_y = self.crop_y, self.crop_x




        if self.flip_x:
            # do a flip
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
        if self.flip_y:
            # do a flip
            img = img.transpose(Image.FLIP_TOP_BOTTOM)

        # randomly apply a blur up to 0.5% of the size of the min (width, height)
        min_size = min(img.width, img.height)
        blur_radius = int(min_size * random.random() * 0.005)
        img = img.filter(ImageFilter.GaussianBlur(radius=blur_radius))

        # make grayscale
        img = img.convert('L')

        if self.dataset_config.buckets:
            # scale and crop based on file item
            img = img.resize((self.scale_to_width, self.scale_to_height), Image.BICUBIC)
            # img = transforms.CenterCrop((self.crop_height, self.crop_width))(img)
            # crop
            img = img.crop((
                self.crop_x,
                self.crop_y,
                self.crop_x + self.crop_width,
                self.crop_y + self.crop_height
            ))
        else:
            raise Exception("Mask images not supported for non-bucket datasets")

        transform = transforms.Compose([
            transforms.ToTensor(),
        ])
        if self.aug_replay_spatial_transforms:
            self.mask_tensor = self.augment_spatial_control(img, transform=transform)
        else:
            self.mask_tensor = transform(img)
        self.mask_tensor = value_map(self.mask_tensor, 0, 1.0, self.mask_min_value, 1.0)
        # convert to grayscale

    def cleanup_mask(self: 'FileItemDTO'):
        self.mask_tensor = None


class UnconditionalFileItemDTOMixin:
    def __init__(self: 'FileItemDTO', *args, **kwargs):
        if hasattr(super(), '__init__'):
            super().__init__(*args, **kwargs)
        self.has_unconditional = False
        self.unconditional_path: Union[str, None] = None
        self.unconditional_tensor: Union[torch.Tensor, None] = None
        self.unconditional_latent: Union[torch.Tensor, None] = None
        self.unconditional_transforms = self.dataloader_transforms
        dataset_config: 'DatasetConfig' = kwargs.get('dataset_config', None)

        if dataset_config.unconditional_path is not None:
            # we are using control images
            img_path = kwargs.get('path', None)
            img_ext_list = ['.jpg', '.jpeg', '.png', '.webp']
            file_name_no_ext = os.path.splitext(os.path.basename(img_path))[0]
            for ext in img_ext_list:
                if os.path.exists(os.path.join(dataset_config.unconditional_path, file_name_no_ext + ext)):
                    self.unconditional_path = os.path.join(dataset_config.unconditional_path, file_name_no_ext + ext)
                    self.has_unconditional = True
                    break

    def load_unconditional_image(self: 'FileItemDTO'):
        try:
            img = Image.open(self.unconditional_path)
            img = exif_transpose(img)
        except Exception as e:
            print(f"Error: {e}")
            print(f"Error loading image: {self.mask_path}")

        img = img.convert('RGB')
        w, h = img.size
        if w > h and self.scale_to_width < self.scale_to_height:
            # throw error, they should match
            raise ValueError(
                f"unexpected values: w={w}, h={h}, file_item.scale_to_width={self.scale_to_width}, file_item.scale_to_height={self.scale_to_height}, file_item.path={self.path}")
        elif h > w and self.scale_to_height < self.scale_to_width:
            # throw error, they should match
            raise ValueError(
                f"unexpected values: w={w}, h={h}, file_item.scale_to_width={self.scale_to_width}, file_item.scale_to_height={self.scale_to_height}, file_item.path={self.path}")

        if self.flip_x:
            # do a flip
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
        if self.flip_y:
            # do a flip
            img = img.transpose(Image.FLIP_TOP_BOTTOM)

        if self.dataset_config.buckets:
            # scale and crop based on file item
            img = img.resize((self.scale_to_width, self.scale_to_height), Image.BICUBIC)
            # img = transforms.CenterCrop((self.crop_height, self.crop_width))(img)
            # crop
            img = img.crop((
                self.crop_x,
                self.crop_y,
                self.crop_x + self.crop_width,
                self.crop_y + self.crop_height
            ))
        else:
            raise Exception("Unconditional images are not supported for non-bucket datasets")

        if self.aug_replay_spatial_transforms:
            self.unconditional_tensor = self.augment_spatial_control(img, transform=self.unconditional_transforms)
        else:
            self.unconditional_tensor = self.unconditional_transforms(img)

    def cleanup_unconditional(self: 'FileItemDTO'):
        self.unconditional_tensor = None
        self.unconditional_latent = None


class PoiFileItemDTOMixin:
    # Point of interest bounding box. Allows for dynamic cropping without cropping out the main subject
    # items in the poi will always be inside the image when random cropping
    def __init__(self: 'FileItemDTO', *args, **kwargs):
        if hasattr(super(), '__init__'):
            super().__init__(*args, **kwargs)
        # poi is a name of the box point of interest in the caption json file
        dataset_config = kwargs.get('dataset_config', None)
        path = kwargs.get('path', None)
        self.poi: Union[str, None] = dataset_config.poi
        self.has_point_of_interest = self.poi is not None
        self.poi_x: Union[int, None] = None
        self.poi_y: Union[int, None] = None
        self.poi_width: Union[int, None] = None
        self.poi_height: Union[int, None] = None

        if self.poi is not None:
            # make sure latent caching is off
            if dataset_config.cache_latents or dataset_config.cache_latents_to_disk:
                raise Exception(
                    f"Error: poi is not supported when caching latents. Please set cache_latents and cache_latents_to_disk to False in the dataset config"
                )
                # make sure we are loading through json
            if dataset_config.caption_ext != 'json':
                raise Exception(
                    f"Error: poi is only supported when using json captions. Please set caption_ext to json in the dataset config"
                )
            self.poi = self.poi.strip()
            # get the caption path
            file_path_no_ext = os.path.splitext(path)[0]
            caption_path = file_path_no_ext + '.json'
            if not os.path.exists(caption_path):
                raise Exception(f"Error: caption file not found for poi: {caption_path}")
            with open(caption_path, 'r', encoding='utf-8') as f:
                json_data = json.load(f)
            if 'poi' not in json_data:
                print(f"Warning: poi not found in caption file: {caption_path}")
            if self.poi not in json_data['poi']:
                print(f"Warning: poi not found in caption file: {caption_path}")
            # poi has, x, y, width, height
            # do full image if no poi
            self.poi_x = 0
            self.poi_y = 0
            self.poi_width = self.width
            self.poi_height = self.height
            try:
                if self.poi in json_data['poi']:
                    poi = json_data['poi'][self.poi]
                    self.poi_x = int(poi['x'])
                    self.poi_y = int(poi['y'])
                    self.poi_width = int(poi['width'])
                    self.poi_height = int(poi['height'])
            except Exception as e:
                pass

            # handle flipping
            if kwargs.get('flip_x', False):
                # flip the poi
                self.poi_x = self.width - self.poi_x - self.poi_width
            if kwargs.get('flip_y', False):
                # flip the poi
                self.poi_y = self.height - self.poi_y - self.poi_height

    def setup_poi_bucket(self: 'FileItemDTO'):
        initial_width = int(self.width * self.dataset_config.scale)
        initial_height = int(self.height * self.dataset_config.scale)
        # we are using poi, so we need to calculate the bucket based on the poi

        # if img resolution is less than dataset resolution, just return and let the normal bucketing happen
        img_resolution = get_resolution(initial_width, initial_height)
        if img_resolution <= self.dataset_config.resolution:
            return False  # will trigger normal bucketing

        bucket_tolerance = self.dataset_config.bucket_tolerance
        poi_x = int(self.poi_x * self.dataset_config.scale)
        poi_y = int(self.poi_y * self.dataset_config.scale)
        poi_width = int(self.poi_width * self.dataset_config.scale)
        poi_height = int(self.poi_height * self.dataset_config.scale)

        # loop to keep expanding until we are at the proper resolution. This is not ideal, we can probably handle it better
        num_loops = 0
        while True:
            # crop left
            if poi_x > 0:
                poi_x = random.randint(0, poi_x)
            else:
                poi_x = 0

            # crop right
            cr_min = poi_x + poi_width
            if cr_min < initial_width:
                crop_right = random.randint(poi_x + poi_width, initial_width)
            else:
                crop_right = initial_width

            poi_width = crop_right - poi_x

            if poi_y > 0:
                poi_y = random.randint(0, poi_y)
            else:
                poi_y = 0

            if poi_y + poi_height < initial_height:
                crop_bottom = random.randint(poi_y + poi_height, initial_height)
            else:
                crop_bottom = initial_height

            poi_height = crop_bottom - poi_y
            try:
                # now we have our random crop, but it may be smaller than resolution. Check and expand if needed
                current_resolution = get_resolution(poi_width, poi_height)
            except Exception as e:
                print(f"Error: {e}")
                print(f"Error getting resolution: {self.path}")
                raise e
                return False
            if current_resolution >= self.dataset_config.resolution:
                # We can break now
                break
            else:
                num_loops += 1
                if num_loops > 100:
                    print(
                        f"Warning: poi bucketing looped too many times. This should not happen. Please report this issue.")
                    return False

        new_width = poi_width
        new_height = poi_height

        bucket_resolution = get_bucket_for_image_size(
            new_width, new_height,
            resolution=self.dataset_config.resolution,
            divisibility=bucket_tolerance
        )

        width_scale_factor = bucket_resolution["width"] / new_width
        height_scale_factor = bucket_resolution["height"] / new_height
        # Use the maximum of the scale factors to ensure both dimensions are scaled above the bucket resolution
        max_scale_factor = max(width_scale_factor, height_scale_factor)

        self.scale_to_width = math.ceil(initial_width * max_scale_factor)
        self.scale_to_height = math.ceil(initial_height * max_scale_factor)
        self.crop_width = bucket_resolution['width']
        self.crop_height = bucket_resolution['height']
        self.crop_x = int(poi_x * max_scale_factor)
        self.crop_y = int(poi_y * max_scale_factor)

        if self.scale_to_width < self.crop_x + self.crop_width or self.scale_to_height < self.crop_y + self.crop_height:
            # todo look into this. This still happens sometimes
            print('size mismatch')

        return True


class ArgBreakMixin:
    # just stops super calls form hitting object
    def __init__(self, *args, **kwargs):
        pass


class LatentCachingFileItemDTOMixin:
    def __init__(self, *args, **kwargs):
        # if we have super, call it
        if hasattr(super(), '__init__'):
            super().__init__(*args, **kwargs)
        self._encoded_latent: Union[torch.Tensor, None] = None
        self._latent_path: Union[str, None] = None
        self.is_latent_cached = False
        self.is_caching_to_disk = False
        self.is_caching_to_memory = False
        self.latent_load_device = 'cpu'
        # sd1 or sdxl or others
        self.latent_space_version = 'sd1'
        # todo, increment this if we change the latent format to invalidate cache
        self.latent_version = 1

    def get_latent_info_dict(self: 'FileItemDTO'):
        item = OrderedDict([
            ("filename", os.path.basename(self.path)),
            ("scale_to_width", self.scale_to_width),
            ("scale_to_height", self.scale_to_height),
            ("crop_x", self.crop_x),
            ("crop_y", self.crop_y),
            ("crop_width", self.crop_width),
            ("crop_height", self.crop_height),
            ("latent_space_version", self.latent_space_version),
            ("latent_version", self.latent_version),
        ])
        # when adding items, do it after so we dont change old latents
        if self.flip_x:
            item["flip_x"] = True
        if self.flip_y:
            item["flip_y"] = True
        return item

    def get_latent_path(self: 'FileItemDTO', recalculate=False):
        if self._latent_path is not None and not recalculate:
            return self._latent_path
        else:
            # we store latents in a folder in same path as image called _latent_cache
            img_dir = os.path.dirname(self.path)
            latent_dir = os.path.join(img_dir, '_latent_cache')
            hash_dict = self.get_latent_info_dict()
            filename_no_ext = os.path.splitext(os.path.basename(self.path))[0]
            # get base64 hash of md5 checksum of hash_dict
            hash_input = json.dumps(hash_dict, sort_keys=True).encode('utf-8')
            hash_str = base64.urlsafe_b64encode(hashlib.md5(hash_input).digest()).decode('ascii')
            hash_str = hash_str.replace('=', '')
            self._latent_path = os.path.join(latent_dir, f'{filename_no_ext}_{hash_str}.safetensors')

        return self._latent_path

    def cleanup_latent(self):
        if self._encoded_latent is not None:
            if not self.is_caching_to_memory:
                # we are caching on disk, don't save in memory
                self._encoded_latent = None
            else:
                # move it back to cpu
                self._encoded_latent = self._encoded_latent.to('cpu')

    def get_latent(self, device=None):
        if not self.is_latent_cached:
            return None
        if self._encoded_latent is None:
            # load it from disk
            state_dict = load_file(
                self.get_latent_path(),
                # device=device if device is not None else self.latent_load_device
                device='cpu'
            )
            self._encoded_latent = state_dict['latent']
        return self._encoded_latent


class LatentCachingMixin:
    def __init__(self: 'AiToolkitDataset', **kwargs):
        # if we have super, call it
        if hasattr(super(), '__init__'):
            super().__init__(**kwargs)
        self.latent_cache = {}

    def cache_latents_all_latents(self: 'AiToolkitDataset'):
        print(f"Caching latents for {self.dataset_path}")
        # cache all latents to disk
        to_disk = self.is_caching_latents_to_disk
        to_memory = self.is_caching_latents_to_memory

        if to_disk:
            print(" - Saving latents to disk")
        if to_memory:
            print(" - Keeping latents in memory")
        # move sd items to cpu except for vae
        self.sd.set_device_state_preset('cache_latents')

        # use tqdm to show progress
        i = 0
        for file_item in tqdm(self.file_list, desc=f'Caching latents{" to disk" if to_disk else ""}'):
            # set latent space version
            if self.sd.is_xl:
                file_item.latent_space_version = 'sdxl'
            else:
                file_item.latent_space_version = 'sd1'
            file_item.is_caching_to_disk = to_disk
            file_item.is_caching_to_memory = to_memory
            file_item.latent_load_device = self.sd.device

            latent_path = file_item.get_latent_path(recalculate=True)
            # check if it is saved to disk already
            if os.path.exists(latent_path):
                if to_memory:
                    # load it into memory
                    state_dict = load_file(latent_path, device='cpu')
                    file_item._encoded_latent = state_dict['latent'].to('cpu', dtype=self.sd.torch_dtype)
            else:
                # not saved to disk, calculate
                # load the image first
                file_item.load_and_process_image(self.transform, only_load_latents=True)
                dtype = self.sd.torch_dtype
                device = self.sd.device_torch
                # add batch dimension
                imgs = file_item.tensor.unsqueeze(0).to(device, dtype=dtype)
                latent = self.sd.encode_images(imgs).squeeze(0)
                # save_latent
                if to_disk:
                    state_dict = OrderedDict([
                        ('latent', latent.clone().detach().cpu()),
                    ])
                    # metadata
                    meta = get_meta_for_safetensors(file_item.get_latent_info_dict())
                    os.makedirs(os.path.dirname(latent_path), exist_ok=True)
                    save_file(state_dict, latent_path, metadata=meta)

                if to_memory:
                    # keep it in memory
                    file_item._encoded_latent = latent.to('cpu', dtype=self.sd.torch_dtype)

                del imgs
                del latent
                del file_item.tensor

                flush(garbage_collect=False)
            file_item.is_latent_cached = True
            i += 1
            # flush every 100
            # if i % 100 == 0:
            #     flush()

        # restore device state
        self.sd.restore_device_state()
