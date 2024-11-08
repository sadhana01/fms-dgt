"""
MIT License

Copyright (c) 2020 EleutherAI

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

# Standard
from typing import Any, Dict, List, Optional, Union
import abc
import hashlib
import json
import os

# Third Party
from sqlitedict import SqliteDict
from tqdm import tqdm

# Local
from fms_dgt.base.block import DATASET_ROW_TYPE, DATASET_TYPE, BaseBlock
from fms_dgt.base.instance import Instance
from fms_dgt.utils import sdg_logger

MODEL_ID_OR_PATH = "model_id_or_path"


class LMGenerator(BaseBlock):
    """Class for LLM Generators"""

    GENERATE = "generate"
    LOGLIKELIHOOD = "loglikelihood"

    def __init__(
        self,
        model_id_or_path: str = None,
        decoding_method: str = "sample",
        truncate: bool = False,
        max_new_tokens: int = None,
        min_new_tokens: int = None,
        max_length: int = 2049,
        random_seed: int = None,
        stop_sequences: List[str] = None,
        temperature: float = None,
        batch_size: int = None,
        auto_chat_template: Optional[Union[bool, Dict]] = False,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)

        self._rank = 0
        self.cache_hook = CacheHook(None)

        self.model_id_or_path: str = model_id_or_path
        assert (
            self.model_id_or_path is not None
        ), f"Must specify model for Generator {self.name}"

        self._decoding_method = decoding_method
        self._truncate = truncate
        self._max_new_tokens = max_new_tokens
        self._min_new_tokens = min_new_tokens
        self._max_length = max_length
        self._random_seed = random_seed
        self._stop_sequences = stop_sequences
        self._temperature = temperature
        self._batch_size = batch_size

        cfg_kwargs = dict()
        for k, v in {
            "decoding_method": self._decoding_method,
            "max_new_tokens": self._max_new_tokens,
            "min_new_tokens": self._min_new_tokens,
            "random_seed": self._random_seed,
            "stop_sequences": self._stop_sequences,
            "temperature": self._temperature,
        }.items():
            if v is not None:
                cfg_kwargs[k] = v

        self._base_kwargs = cfg_kwargs

        self._chat_template = None
        if auto_chat_template:

            if auto_chat_template is True:
                auto_chat_template = dict()
            assert isinstance(
                auto_chat_template, dict
            ), f"'auto_chat_template' must either be boolean or dictionary, instead got '{auto_chat_template}' with type {type(auto_chat_template)}"

            self._auto_chat_template_params = {"tokenize": False, **auto_chat_template}

            try:
                # Third Party
                from transformers import AutoTokenizer
            except ModuleNotFoundError:
                raise ModuleNotFoundError(
                    "In order to enable 'auto_chat_template', ",
                    "please install transformers via `pip install transformers`",
                )
            self._chat_template = AutoTokenizer.from_pretrained(model_id_or_path)

    @property
    def rank(self):
        # used in the case of parallelism. Hardcoded to
        # ensure no errors arise using API models which do
        # not support multi-device parallelism nor expect it.
        return self._rank

    @property
    def max_length(self) -> int:
        # Note: the OpenAI API supports up to 2049 tokens, with the first token being the first input token
        return self._max_length

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @property
    def random_seed(self) -> int:
        return self._random_seed

    def update_instance_with_result(
        self,
        method: str,
        res: Any,
        instance: Instance,
        until: Optional[List[str]] = None,
    ):
        if until is not None and type(res) == str:
            for term in until:
                if len(term) > 0:
                    res = res.split(term)[0]
        instance.result = res
        self.cache_hook.add_partial(method, instance, res)

    @abc.abstractmethod
    def generate_batch(
        self, requests: List[Instance], **kwargs: Union[str, Dict]
    ) -> None:
        pass

    @abc.abstractmethod
    def loglikelihood_batch(
        self, requests: List[Instance], disable_tqdm: bool = False
    ) -> None:
        pass

    def set_cache_hook(self, cache_hook) -> None:
        self.cache_hook = cache_hook

    def execute(
        self,
        inputs: DATASET_TYPE,
        *,
        arg_fields: Optional[List[str]] = None,
        kwarg_fields: Optional[List[str]] = None,
        result_field: Optional[str] = None,
        method: str = GENERATE,
        **kwargs: Any,
    ):

        # simplify generation here
        instances: List[Instance] = []
        for inp in inputs:
            inp_args, inp_kwargs = self.get_args_kwargs(
                inp, method, arg_fields, kwarg_fields
            )
            instances.append(Instance(args=inp_args, kwargs=inp_kwargs, data=inp))

        if method == self.GENERATE:
            self.generate_batch(
                instances,
                **kwargs,
            )
        elif method == self.LOGLIKELIHOOD:
            self.loglikelihood_batch(
                instances,
                **kwargs,
            )
        else:
            err_str = (
                f"Unhandled method type: {method}"
                if method is not None
                else f"Must set 'method' kwarg to '{self.GENERATE}' or '{self.LOGLIKELIHOOD}'"
            )
            raise ValueError(err_str)

        outputs = []
        for inst in instances:
            self.write_result(inst.data, inst.result, result_field)
            outputs.append(inst.data)

        return outputs

    def get_args_kwargs(
        self,
        inp: DATASET_ROW_TYPE,
        method: str,
        arg_fields: Optional[List[str]] = None,
        kwarg_fields: Optional[List[str]] = None,
    ):

        assert method in [
            self.GENERATE,
            self.LOGLIKELIHOOD,
        ], f"'method' value should be one of [{self.GENERATE}, {self.LOGLIKELIHOOD}], instead it was given as {method}"
        inp_args, inp_kwargs = super().get_args_kwargs(inp, arg_fields, kwarg_fields)

        # double check that model specified in kwargs (if it is specified in kwargs) matches model defined for chat template
        if (
            method == self.GENERATE
            and self._chat_template is not None
            and (
                inp_kwargs.get(MODEL_ID_OR_PATH, self.model_id_or_path)
                == self.model_id_or_path
            )
        ):

            prompt = inp_args[0]
            assert type(prompt) in [
                list,
                str,
            ], f"Prompt must be given as either List[Dict] or str, but was instead given as {type(prompt)}"

            if type(prompt) == str:
                prompt = [{"role": "user", "content": prompt}]

            inp_args = [
                self._chat_template.apply_chat_template(
                    prompt, **self._auto_chat_template_params
                )
            ]

        return inp_args, inp_kwargs

    def init_model(self, *args: Any, **kwargs: Any):
        pass

    def release_model(self):
        pass


### SQLite-based caching of LM responses
def hash_args(attr, request):
    dat = json.dumps([attr] + [request.args, request.kwargs])
    return hashlib.sha256(dat.encode("utf-8")).hexdigest()


class CacheHook:
    def __init__(self, cachinglm) -> None:
        if cachinglm is None:
            self.dbdict = None
            return

        self.dbdict: SqliteDict = cachinglm.dbdict

    def add_partial(self, attr, req, res) -> None:
        if self.dbdict is None:
            return
        hsh = hash_args(attr, req)
        self.dbdict[hsh] = res


class CachingLM:
    def __init__(self, lm: LMGenerator, cache_db) -> None:
        """LM wrapper that returns cached results if they exist, and uses the underlying LM if not.

        :param lm: LM
            Underlying LM
        :param cache_db: str
            Path to cache db
        """
        self.lm = lm
        self.cache_db = cache_db
        if os.path.dirname(cache_db):
            os.makedirs(os.path.dirname(cache_db), exist_ok=True)
        self.dbdict = SqliteDict(cache_db, autocommit=True)

        # add hook to lm
        self.lm.set_cache_hook(self.get_cache_hook())

        self.dbdict

    def __getattr__(self, attr):
        lm_attr = getattr(self.lm, attr)

        if not callable(lm_attr):
            return lm_attr
        elif attr in ["init_model", "release_model"]:
            return lm_attr

        def fn(requests: List[Instance]):
            res = []
            remaining_reqs = []
            warned = False
            # figure out which ones are cached and which ones are new
            sdg_logger.info(
                "Loading '%s' responses from cache '%s' where possible...",
                attr,
                self.cache_db,
            )
            for req in tqdm(requests, desc="Checking cached requests"):
                hsh = hash_args(attr, req)
                if (
                    attr == "generate_batch"
                    and req.kwargs.get("decoding_method", None) == "sample"
                ):
                    # when we are doing non-greedy generation, don't use the cache
                    # (else every "randomly sampled" generation would be identical for repeats > 1).
                    if not warned:
                        sdg_logger.warning(
                            "Arguments to lm.generate_batch() '%s' include non-deterministic "
                            "sampling. Caching will not be performed for such requests.",
                            req.kwargs,
                        )
                        warned = True
                    res.append(None)
                    remaining_reqs.append(req)
                elif hsh in self.dbdict:
                    ob = self.dbdict[hsh]
                    assert ob is not None
                    res.append(ob)
                else:
                    res.append(None)
                    remaining_reqs.append(req)

            sdg_logger.info(
                "Cached requests: %s, Requests remaining: %s",
                len(requests) - len(remaining_reqs),
                len(remaining_reqs),
            )

            # actually run the LM on the requests that do not have cached results
            getattr(self.lm, attr)(remaining_reqs)

            # stick the new ones back into the list and also cache any of the new ones
            resptr = 0
            for req in remaining_reqs:
                while res[resptr] is not None:
                    resptr += 1

                res[resptr] = req.result

                # caching
                hsh = hash_args(attr, req)
                self.dbdict[hsh] = req.result
            self.dbdict.commit()

            # now we store result
            for req, req_res in zip(requests, res):
                req.result = req_res

        return fn

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.execute(*args, **kwargs)

    def execute(
        self,
        inputs: DATASET_TYPE,
        arg_fields: Optional[List[str]] = None,
        kwarg_fields: Optional[List[str]] = None,
        result_field: Optional[str] = None,
        method: str = "generate",
        **kwargs: Any,
    ) -> None:

        # simplify generation here
        instances: List[Instance] = []
        for inp in inputs:
            inp_args, inp_kwargs = self.lm.get_args_kwargs(
                inp, method, arg_fields, kwarg_fields
            )
            instances.append(Instance(args=inp_args, kwargs=inp_kwargs, data=inp))

        if method == self.lm.GENERATE:
            self.generate_batch(
                instances,
                **kwargs,
            )
        elif method == self.lm.LOGLIKELIHOOD:
            self.loglikelihood_batch(
                instances,
                **kwargs,
            )
        else:
            err_str = (
                f"Unhandled method type: {method}"
                if method is not None
                else f"Must set 'method' kwarg to '{self.lm.GENERATE}' or '{self.lm.LOGLIKELIHOOD}'"
            )
            raise ValueError(err_str)

        outputs = []
        for inst in instances:
            self.lm.write_result(inst.data, inst.result, result_field)
            outputs.append(inst.data)

        return outputs

    def get_cache_hook(self):
        return CacheHook(self)
