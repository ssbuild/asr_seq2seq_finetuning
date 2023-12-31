# @Time    : 2023/1/22 16:22
# @Author  : tk
# @FileName: data_utils.py
import glob
import sys
import os
from functools import cache

sys.path.append(os.path.join(os.path.dirname(__file__)))

import copy
import json
import random
import typing
import numpy as np
import torch
from deep_training.data_helper import DataHelper, ModelArguments, TrainingArguments, TrainingArgumentsHF, \
    TrainingArgumentsCL, DataArguments, TrainingArgumentsAC
from aigc_zoo.model_zoo.asr_seq2seq.llm_model import PetlArguments,LoraConfig,PromptArguments
from fastdatasets.record import load_dataset as Loader, RECORD, WriterObject, gfile
from transformers import PreTrainedTokenizer, HfArgumentParser, PretrainedConfig
from data_processer import TokenIdsMaker
from config import *
from module_setup import module_setup


module_setup()


def preprocess(text):
  return text

def postprocess(text):
  return text


class NN_DataHelper(DataHelper):
    index = 1
    forward_attention_mask = False
    decoder_start_token_id = None
    def __init__(self, *args, **kwargs):
        super(NN_DataHelper, self).__init__(*args, **kwargs)

    def load_tokenizer_and_config(self, *args, **kwargs):
        ret = super().load_tokenizer_and_config(*args, **kwargs)
        self._preprocess_tokenizer_config()
        self.load_processer()
        self.load_feature_extractor()
        return ret

    def _preprocess_tokenizer_config(self):
        config = self.config
        self.forward_attention_mask = (
                getattr(config, "model_type", None) == "whisper"
                and getattr(config, "apply_spec_augment", False)
                and getattr(config, "mask_time_prob", 0) > 0
        )
        language = self.data_args.data_custom["language"]
        task = self.data_args.data_custom["task"]
        if language is not None:
            # We only need to set the task id when the language is specified (i.e. in a multilingual setting)
            tokenizer.set_prefix_tokens(language=language, task=task)

        self.decoder_start_token_id = config.decoder_start_token_id
    def on_data_ready(self):
        self.index = -1

    # 切分词
    def on_data_process(self, data: typing.Any, mode: str):
        self.index += 1

        tokenizer: PreTrainedTokenizer
        config = self.config
        max_seq_length = self.max_seq_length_dict[mode]
        tokenizer = self.tokenizer
        feature_extractor = self.feature_extractor
        data_args = self.data_args
        examples = data


        d = TokenIdsMaker.process(data_args,
                tokenizer,
                config,
                max_seq_length,
                feature_extractor,
                self.forward_attention_mask,
                examples)

        if not d:
            return None

        if self.index < 3:
            print(d)
        return d

    def _get_paragraph(self,lines):
        D = []
        for line_id, line in enumerate(lines):
            jd = json.loads(line)
            if not jd:
                continue
            D.append((jd["path"],jd["sentence"]))
        return D

    # 读取文件
    def on_get_corpus(self, files: typing.List, mode: str):
        D = []
        files = sum([glob.glob(file) for file in files], [])
        for file in files:
            with open(file, mode='r', encoding='utf-8', newline='\n') as f:
                lines = f.readlines()
            D.extend(self._get_paragraph(lines))
        return D

    def collate_fn(self, batch):
        batch = copy.copy(batch)
        model_input_name = "input_features"
        input_shape = [np.asarray(feature["shape"],dtype=np.int64) for feature in batch]
        input_features = [{model_input_name: np.asarray(feature[model_input_name],dtype=np.float32).reshape(input_shape[i])} for i,feature in enumerate(batch)]
        label_features = [{"input_ids": feature["labels"]} for feature in batch]

        o = self.processor.feature_extractor.pad(input_features, return_tensors="pt")

        if self.forward_attention_mask:
            o["attention_mask"] = torch.LongTensor([feature["attention_mask"] for feature in batch])

        labels_batch = self.processor.tokenizer.pad(label_features, return_tensors="pt")

        # replace padding with -100 to ignore loss correctly
        labels = labels_batch["input_ids"].masked_fill(labels_batch.attention_mask.ne(1), -100)

        # if bos token is appended in previous tokenization step,
        # cut bos token here as it's append later anyways
        if (labels[:, 0] == self.decoder_start_token_id).all().cpu().item():
            labels = labels[:, 1:]

        o["labels"] = labels
        return o

    def make_dataset_all(self):
        data_args = self.data_args
        # schema for arrow parquet
        schema = {
            "input_features": "float32_list",
            "shape": "int32_list",
            "labels": "int32_list",
        }
        if self.forward_attention_mask:
            schema["attention_mask"] = "int32_list"

        # 缓存数据集
        if data_args.do_train:
            self.make_dataset_with_args(data_args.train_file, mixed_data=False, shuffle=True, mode='train',
                                        schema=schema)
        if data_args.do_eval:
            self.make_dataset_with_args(data_args.eval_file, mode='eval', schema=schema)
        if data_args.do_test:
            self.make_dataset_with_args(data_args.test_file, mode='test', schema=schema)

        # 记录缓存文件
        with open(os.path.join(data_args.output_dir, 'intermediate_file_index.json'), mode='w',
                  encoding='utf-8') as f:
            f.write(json.dumps({
                "train_files": self.train_files,
                "eval_files": self.eval_files,
                "test_files": self.test_files,
            }, ensure_ascii=False))

    @cache
    def load_dataset_files(self):
        data_args = self.data_args

        if not data_args.convert_file:
            return {
                "train_files": self.train_files,
                "eval_files": self.eval_files,
                "test_files": self.test_files,
            }

        filename = os.path.join(data_args.output_dir, 'intermediate_file_index.json')
        assert os.path.exists(filename), 'make you dataset firstly'
        with open(filename, mode='r', encoding='utf-8') as f:
            return json.loads(f.read())

if __name__ == '__main__':

    if global_args["trainer_backend"] == "hf":
        parser = HfArgumentParser((ModelArguments, TrainingArgumentsHF, DataArguments, PetlArguments, PromptArguments),
                                  conflict_handler='resolve')
        model_args, training_args, data_args, lora_args, prompt_args = parser.parse_dict(train_info_args,
                                                                                         allow_extra_keys=True, )
    elif global_args["trainer_backend"] == "pl":
        parser = HfArgumentParser((ModelArguments, TrainingArguments, DataArguments, PetlArguments, PromptArguments))
        model_args, training_args, data_args, _, _ = parser.parse_dict(train_info_args)
    elif global_args["trainer_backend"] == "cl":
        parser = HfArgumentParser((ModelArguments, TrainingArgumentsCL, DataArguments, PetlArguments, PromptArguments),
                                  conflict_handler='resolve')
        model_args, training_args, data_args, lora_args, prompt_args = parser.parse_dict(train_info_args,
                                                                                         allow_extra_keys=True, )
    else:
        parser = HfArgumentParser((ModelArguments, TrainingArgumentsAC, DataArguments, PetlArguments, PromptArguments),
                                  conflict_handler='resolve')
        model_args, training_args, data_args, lora_args, prompt_args = parser.parse_dict(train_info_args,
                                                                                         allow_extra_keys=True, )


    dataHelper = NN_DataHelper(model_args, training_args, data_args)
    tokenizer, config, _, _ = dataHelper.load_tokenizer_and_config(config_kwargs={"torch_dtype": torch.float16})
    

    # 缓存数据集
    print(f'to make dataset is overwrite_cache {data_args.overwrite_cache}')
    dataHelper.make_dataset_all()

    print('make dataset complete!')
    print('check data !')
    dataset = dataHelper.load_sequential_sampler(dataHelper.load_dataset_files()["train_files"],
                                                 with_load_memory=data_args.data_backend == 'record',
                                                 batch_size=1,
                                                 collate_fn=dataHelper.collate_fn)

    print('total', len(dataset))
    for i, d in enumerate(dataset):
        print(d)
        if i > 3:
            break
