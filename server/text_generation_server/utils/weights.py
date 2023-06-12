from pathlib import Path
from typing import List
from safetensors import safe_open
import torch


class Weights:
    def __init__(self, filenames: List[Path], device, dtype, process_group):
        routing = {}
        for filename in filenames:
            with safe_open(filename, framework="pytorch") as f:
                for k in f.keys():
                    if k in routing:
                        raise RuntimeError(
                            f"Key {k} was found in multiple files: {filename} and {routing[k]}"
                        )
                    routing[k] = filename
        self.routing = routing
        self.device = device
        self.dtype = dtype
        self.process_group = process_group
        self._handles = {}

    def _get_handle(self, filename):
        if filename not in self._handles:
            f = safe_open(filename, framework="pytorch")
            self._handles[filename] = f

        return self._handles[filename]

    def get_filename(self, tensor_name: str) -> str:
        filename = self.routing.get(tensor_name, None)
        if filename is None:
            raise RuntimeError(f"weight {tensor_name} does not exist")
        return str(filename)

    def _get_slice(self, tensor_name: str):
        filename = self.get_filename(tensor_name)
        f = self._get_handle(filename)
        slice_ = f.get_slice(tensor_name)
        return slice_

    def get_shape(self, tensor_name: str):
        return self._get_slice(tensor_name).get_shape()

    def get_tensor(self, tensor_name: str):
        filename = self.get_filename(tensor_name)
        f = self._get_handle(filename)
        tensor = f.get_tensor(tensor_name)
        # Special case for gptq which shouldn't convert
        # u4 which are disguised as int32
        if tensor.dtype != torch.int32:
            tensor = tensor.to(dtype=self.dtype)
        tensor = tensor.to(device=self.device)
        return tensor

    def get_sharded(self, tensor_name: str, dim: int):
        filename = self.get_filename(tensor_name)
        world_size = self.process_group.size()
        rank = self.process_group.rank()

        f = self._get_handle(filename)
        slice_ = f.get_slice(tensor_name)
        size = slice_.get_shape()[dim]
        block_size = size // world_size
        start = rank * block_size
        stop = (rank + 1) * block_size

        assert (
            size % world_size == 0
        ), f"The choosen size {size} is not compatible with sharding on {world_size} shards"

        if dim == 0:
            tensor = slice_[start:stop]
        elif dim == 1:
            tensor = slice_[:, start:stop]
        else:
            raise NotImplementedError("Let's make that generic when needed")
        # Special case for gptq which shouldn't convert
        # u4 which are disguised as int32
        if tensor.dtype != torch.int32:
            tensor = tensor.to(dtype=self.dtype)
        tensor = tensor.to(device=self.device)
        return tensor

    def get_multi_weights_col(self, prefixes: List[str], quantize: str):
        if quantize == "gptq":
            try:
                qweight = torch.cat([self.get_sharded(f"{p}.qweight", dim=1) for p in prefixes], dim=1)
            except RuntimeError:
                raise RuntimeError("Cannot load `gptq` weight, make sure the model is already quantized, or quantize it with `text-generation-server quantize ORIGINAL_MODEL_ID NEW_MODEL_ID`")

            qzeros = torch.cat([self.get_sharded(f"{p}.qzeros", dim=1) for p in prefixes], dim=1)
            scales = torch.cat([self.get_sharded(f"{p}.scales", dim=1) for p in prefixes], dim=1)
            w = [self.get_tensor(f"{p}.g_idx") for p in prefixes]
            for w2 in w[1:]:
                torch.testing.assert_close(w2, w[0])
            g_idx = w[0]
            # TODO Get that from file to be more generic
            bits = 4
            groupsize = 128
            weight = (qweight, qzeros, scales, g_idx, bits, groupsize)
        else:
            w = [self.get_sharded(f"{p}.weight", dim=0) for p in prefixes]
            weight = torch.cat(w, dim=dim)
        return weight

    def get_multi_self_row(self, prefix: str, quantize: str):
        if quantize == "gptq":
            try:
                qweight = self.get_sharded(f"{prefix}.qweight", dim=0)
            except RuntimeError:
                raise RuntimeError("Cannot load `gptq` weight, make sure the model is already quantized, or quantize it with `text-generation-server quantize ORIGINAL_MODEL_ID NEW_MODEL_ID`")
            qzeros = self.get_tensor(f"{prefix}.qzeros")
            scales = self.get_tensor(f"{prefix}.scales")
            g_idx = self.get_sharded(f"{prefix}.g_idx", dim=0)

            # TODO Get that from file to be more generic
            bits = 4
            groupsize = 128

            weight = (qweight, qzeros, scales, g_idx, bits, groupsize)
        else:
            weight = self.get_sharded(f"{prefix}.weight", dim=1)
        return weight
