import glob
import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import mlx.core as mx
import mlx.nn as nn
import numpy as np
from huggingface_hub import snapshot_download

from .config import ModelConfig
from .language import LanguageModel, RMSNorm
from .vision import VisionModel


class Gemma3MultiModalProjector(nn.Module):
    def __init__(self, config: ModelConfig):
        super().__init__()
        self.mm_input_projection_weight = mx.ones(
            (config.vision_config.hidden_size, config.text_config.hidden_size)
        )

        self.mm_soft_emb_norm = RMSNorm(
            config.vision_config.hidden_size, eps=config.vision_config.layer_norm_eps
        )
        self.patches_per_image = int(
            config.vision_config.image_size // config.vision_config.patch_size
        )
        self.tokens_per_side = int(config.text_config.mm_tokens_per_image**0.5)
        self.kernel_size = self.patches_per_image // self.tokens_per_side
        self.avg_pool = nn.AvgPool2d(
            kernel_size=self.kernel_size, stride=self.kernel_size
        )

    def __call__(self, x: mx.array) -> mx.array:
        b, _, l = x.shape

        reshaped_vision_outputs = x.transpose(0, 2, 1)
        reshaped_vision_outputs = reshaped_vision_outputs.reshape(
            b, l, self.patches_per_image, self.patches_per_image
        )

        # Transpose to place h, w in indices 1, 2
        reshaped_vision_outputs = reshaped_vision_outputs.transpose(0, 2, 3, 1)
        pooled_vision_outputs = self.avg_pool(reshaped_vision_outputs)
        pooled_vision_outputs = pooled_vision_outputs.transpose(0, 3, 1, 2).flatten(2)
        pooled_vision_outputs = pooled_vision_outputs.transpose(0, 2, 1)

        normed_vision_outputs = self.mm_soft_emb_norm(pooled_vision_outputs)

        projected_vision_outputs = mx.einsum(
            "btm,md->btd", normed_vision_outputs, self.mm_input_projection_weight
        )
        return projected_vision_outputs.astype(x.dtype)


def masked_scatter(
    final_embedding: mx.array,
    image_mask_expanded: mx.array,
    scaled_image_features: mx.array,
):
    # Reshape the tensors to 1D
    final_embedding_shape = final_embedding.shape
    scaled_image_features_flattened = mx.flatten(scaled_image_features)
    final_embedding_flattened = mx.flatten(final_embedding)
    image_mask_expanded_flattened = mx.flatten(image_mask_expanded)

    # Scatter the scaled image features into the special image token positions
    image_positions = mx.array(np.where(image_mask_expanded_flattened)[0], mx.uint32)
    final_embedding_flattened[image_positions] = scaled_image_features_flattened

    # Reshape back to the original shape
    final_embedding = mx.reshape(final_embedding_flattened, final_embedding_shape)

    return final_embedding


class Model(nn.Module):
    def __init__(self, config: ModelConfig):
        super().__init__()
        self.model_type = config.model_type
        self.config = config

        self.vision_tower = VisionModel(config.vision_config)
        self.language_model = LanguageModel(config.text_config)
        self.multi_modal_projector = Gemma3MultiModalProjector(config)

    def get_input_embeddings(
        self,
        input_ids: Optional[mx.array] = None,
        pixel_values: Optional[mx.array] = None,
        mask: Optional[mx.array] = None,
    ):
        if pixel_values is None:
            return self.language_model.model.embed_tokens(input_ids), None

        inputs_embeds = self.language_model.model.embed_tokens(input_ids)

        hidden_state, _, _ = self.vision_tower(
            pixel_values.transpose(0, 2, 3, 1).astype(inputs_embeds.dtype),
            output_hidden_states=True,
        )

        image_features = self.multi_modal_projector(hidden_state)

        final_inputs_embeds, final_attention_mask_4d = (
            self.prepare_inputs_for_multimodal(
                self.config.hidden_size,
                self.config.pad_token_id,
                self.config.image_token_index,
                image_features,
                inputs_embeds,
                input_ids,
                mask,
            )
        )
        return final_inputs_embeds, final_attention_mask_4d

    @staticmethod
    def prepare_inputs_for_multimodal(
        hidden_size,
        pad_token_id,
        image_token_index,
        image_features,
        inputs_embeds,
        input_ids,
        attention_mask,
    ):
        _, _, embed_dim = image_features.shape

        batch_size, sequence_length = input_ids.shape
        scaled_image_features = image_features / (hidden_size**0.5)
        final_embedding = mx.zeros((batch_size, sequence_length, embed_dim))

        pad_token_id = pad_token_id
        pad_token_id = pad_token_id if pad_token_id is not None else 0
        text_mask = (input_ids != image_token_index) & (input_ids != pad_token_id)
        image_mask = input_ids == image_token_index
        pad_mask = input_ids == pad_token_id

        # expand masks to match embedding dimension
        text_mask_expanded = mx.expand_dims(text_mask, -1)
        text_mask_expanded = mx.repeat(text_mask_expanded, embed_dim, axis=-1)
        pad_mask_expanded = mx.expand_dims(pad_mask, -1)
        pad_mask_expanded = mx.repeat(pad_mask_expanded, embed_dim, axis=-1)
        image_mask_expanded = mx.expand_dims(image_mask, -1)
        image_mask_expanded = mx.repeat(image_mask_expanded, embed_dim, axis=-1)

        # insert padding and text token embeddings
        final_embedding = mx.where(text_mask_expanded, inputs_embeds, final_embedding)
        final_embedding = mx.where(
            pad_mask_expanded, mx.zeros_like(final_embedding), final_embedding
        )

        # insert image token embeddings
        final_embedding = masked_scatter(
            final_embedding, image_mask_expanded, scaled_image_features
        )

        attention_mask_expanded_1 = mx.expand_dims(attention_mask, 1)
        attention_mask_expanded_2 = mx.expand_dims(attention_mask, 2)
        final_attention_mask_4d = attention_mask_expanded_1 * attention_mask_expanded_2
        final_attention_mask_4d = final_attention_mask_4d
        final_attention_mask_4d = mx.expand_dims(final_attention_mask_4d, 1)
        final_embedding = mx.array(final_embedding)
        return final_embedding.astype(inputs_embeds.dtype), final_attention_mask_4d

    @property
    def layers(self):
        return self.language_model.model.layers

    def __call__(
        self,
        input_ids: mx.array,
        pixel_values: mx.array,
        mask: Optional[mx.array] = None,
        cache: Optional[mx.array] = None,
        **kwargs,
    ):
        input_embeddings, final_attention_mask_4d = self.get_input_embeddings(
            input_ids, pixel_values, mask
        )

        logits = self.language_model(
            inputs=input_ids,
            cache=cache,
            inputs_embeds=input_embeddings,
        )
        return logits
