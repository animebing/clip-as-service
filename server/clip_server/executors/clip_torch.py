import os
import warnings
from multiprocessing.pool import ThreadPool
from typing import Optional, Dict

import numpy as np
import torch
from clip_server.executors.helper import (
    split_img_txt_da,
    preproc_image,
    preproc_text,
    set_rank,
)
from clip_server.model import clip
from clip_server.model.clip_model import CLIPModel
from clip_server.model.tokenization import Tokenizer
from jina import Executor, requests, DocumentArray


class CLIPEncoder(Executor):
    def __init__(
        self,
        name: str = 'ViT-B-32::openai',
        device: Optional[str] = None,
        jit: bool = False,
        num_worker_preprocess: int = 4,
        minibatch_size: int = 32,
        access_paths: str = '@r',
        **kwargs,
    ):
        super().__init__(**kwargs)

        self._minibatch_size = minibatch_size
        self._access_paths = access_paths
        if 'traversal_paths' in kwargs:
            warnings.warn(
                f'`traversal_paths` is deprecated. Use `access_paths` instead.'
            )
            self._access_paths = kwargs['traversal_paths']

        if not device:
            self._device = 'cuda' if torch.cuda.is_available() else 'cpu'
        else:
            self._device = device

        if not self._device.startswith('cuda') and (
            'OMP_NUM_THREADS' not in os.environ
            and hasattr(self.runtime_args, 'replicas')
        ):
            replicas = getattr(self.runtime_args, 'replicas', 1)
            num_threads = max(1, torch.get_num_threads() // replicas)
            if num_threads < 2:
                warnings.warn(
                    f'Too many replicas ({replicas}) vs too few threads {num_threads} may result in '
                    f'sub-optimal performance.'
                )

            # NOTE: make sure to set the threads right after the torch import,
            # and `torch.set_num_threads` always take precedence over environment variables `OMP_NUM_THREADS`.
            # For more details, please see https://pytorch.org/docs/stable/generated/torch.set_num_threads.html
            torch.set_num_threads(max(num_threads, 1))
            torch.set_num_interop_threads(1)
        self._pool = ThreadPool(processes=num_worker_preprocess)

        self._model = CLIPModel(name, device=self._device, jit=jit, **kwargs)
        self._tokenizer = Tokenizer(name)
        self._image_transform = clip._transform_ndarray(self._model.image_size)

    def _preproc_images(self, docs: 'DocumentArray'):
        with self.monitor(
            name='preprocess_images_seconds',
            documentation='images preprocess time in seconds',
        ):
            return preproc_image(
                docs,
                preprocess_fn=self._image_transform,
                device=self._device,
                return_np=False,
            )

    def _preproc_texts(self, docs: 'DocumentArray'):
        with self.monitor(
            name='preprocess_texts_seconds',
            documentation='texts preprocess time in seconds',
        ):
            return preproc_text(
                docs, tokenizer=self._tokenizer, device=self._device, return_np=False
            )

    @requests(on='/rank')
    async def rank(self, docs: 'DocumentArray', parameters: Dict, **kwargs):
        await self.encode(docs['@r,m'])

        set_rank(docs)

    @requests
    async def encode(self, docs: 'DocumentArray', parameters: Dict = {}, **kwargs):
        access_paths = parameters.get('access_paths', self._access_paths)
        if 'traversal_paths' in parameters:
            warnings.warn(
                f'`traversal_paths` is deprecated. Use `access_paths` instead.'
            )
            access_paths = parameters['traversal_paths']

        _img_da = DocumentArray()
        _txt_da = DocumentArray()
        for d in docs[access_paths]:
            split_img_txt_da(d, _img_da, _txt_da)

        with torch.inference_mode():
            # for image
            if _img_da:
                for minibatch, batch_data in _img_da.map_batch(
                    self._preproc_images,
                    batch_size=self._minibatch_size,
                    pool=self._pool,
                ):
                    with self.monitor(
                        name='encode_images_seconds',
                        documentation='images encode time in seconds',
                    ):
                        minibatch.embeddings = (
                            self._model.encode_image(**batch_data)
                            .cpu()
                            .numpy()
                            .astype(np.float32)
                        )

            # for text
            if _txt_da:
                for minibatch, batch_data in _txt_da.map_batch(
                    self._preproc_texts,
                    batch_size=self._minibatch_size,
                    pool=self._pool,
                ):
                    with self.monitor(
                        name='encode_texts_seconds',
                        documentation='texts encode time in seconds',
                    ):
                        minibatch.embeddings = (
                            self._model.encode_text(**batch_data)
                            .cpu()
                            .numpy()
                            .astype(np.float32)
                        )

        return docs
