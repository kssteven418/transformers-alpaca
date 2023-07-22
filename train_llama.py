import copy
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Sequence

import torch
import torch.optim as optim
import transformers
from torch.utils.data import Dataset
from transformers import Trainer

from tqdm import tqdm
import utils

IGNORE_INDEX = -100
DEFAULT_PAD_TOKEN = "[PAD]"
DEFAULT_EOS_TOKEN = "</s>"
DEFAULT_BOS_TOKEN = "</s>"
DEFAULT_UNK_TOKEN = "</s>"
PROMPT_DICT = {
    "prompt_input": (
        "Below is an instruction that describes a task, paired with an input that provides further context. "
        "Write a response that appropriately completes the request.\n\n"
        "### Instruction:\n{instruction}\n\n### Input:\n{input}\n\n### Response:"
    ),
    "prompt_no_input": (
        "Below is an instruction that describes a task. "
        "Write a response that appropriately completes the request.\n\n"
        "### Instruction:\n{instruction}\n\n### Response:"
    ),
}

PROMPT_DICT_NEW={
    "prompt":(
        "Below is a text imitation task. You will be given a text description and asked to rewrite it in a different style.\n\n"
        "### Input:\n{input}\n\n### Output:"
    )
}

@dataclass
class ModelArguments:
    model_name_or_path: Optional[str] = field(default="facebook/opt-125m")


@dataclass
class DataArguments:
    data_path: str = field(default=None, metadata={"help": "Path to the training data."})


@dataclass
class TrainingArguments(transformers.TrainingArguments):
    cache_dir: Optional[str] = field(default=None)
    optim: str = field(default="adamw_torch")
    model_max_length: int = field(
        default=512,
        metadata={"help": "Maximum sequence length. Sequences will be right padded (and possibly truncated)."},
    )


def safe_save_model_for_hf_trainer(trainer: transformers.Trainer, output_dir: str):
    """Collects the state dict and dump to disk."""
    state_dict = trainer.model.state_dict()
    if trainer.args.should_save:
        cpu_state_dict = {key: value.cpu() for key, value in state_dict.items()}
        del state_dict
        trainer._save(output_dir, state_dict=cpu_state_dict)  # noqa


def smart_tokenizer_and_embedding_resize(
    special_tokens_dict: Dict,
    tokenizer: transformers.PreTrainedTokenizer,
    model: transformers.PreTrainedModel,
):
    """Resize tokenizer and embedding.

    Note: This is the unoptimized version that may make your embedding size not be divisible by 64.
    """
    num_new_tokens = tokenizer.add_special_tokens(special_tokens_dict)
    model.resize_token_embeddings(len(tokenizer))

    if num_new_tokens > 0:
        input_embeddings = model.get_input_embeddings().weight.data
        output_embeddings = model.get_output_embeddings().weight.data

        input_embeddings_avg = input_embeddings[:-num_new_tokens].mean(dim=0, keepdim=True)
        output_embeddings_avg = output_embeddings[:-num_new_tokens].mean(dim=0, keepdim=True)

        input_embeddings[-num_new_tokens:] = input_embeddings_avg
        output_embeddings[-num_new_tokens:] = output_embeddings_avg


def _tokenize_fn(strings: Sequence[str], tokenizer: transformers.PreTrainedTokenizer) -> Dict:
    """Tokenize a list of strings."""
    tokenized_list = [
        tokenizer(
            text,
            return_tensors="pt",
            padding="longest",
            max_length=tokenizer.model_max_length,
            truncation=True,
        )
        for text in strings
    ]
    input_ids = labels = [tokenized.input_ids[0] for tokenized in tokenized_list]
    input_ids_lens = labels_lens = [
        tokenized.input_ids.ne(tokenizer.pad_token_id).sum().item() for tokenized in tokenized_list
    ]
    return dict(
        input_ids=input_ids,
        labels=labels,
        input_ids_lens=input_ids_lens,
        labels_lens=labels_lens,
    )


def preprocess(
    sources: Sequence[str],
    targets: Sequence[str],
    tokenizer: transformers.PreTrainedTokenizer,
) -> Dict:
    """Preprocess the data by tokenizing."""
    examples = [s + t for s, t in zip(sources, targets)]
    examples_tokenized, sources_tokenized = [_tokenize_fn(strings, tokenizer) for strings in (examples, sources)]
    input_ids = examples_tokenized["input_ids"]
    labels = copy.deepcopy(input_ids)
    for label, source_len in zip(labels, sources_tokenized["input_ids_lens"]):
        label[:source_len] = IGNORE_INDEX
    return dict(input_ids=input_ids, labels=labels)


class SupervisedDataset(Dataset):
    """Dataset for supervised fine-tuning."""

    def __init__(self, data_path: str, tokenizer: transformers.PreTrainedTokenizer):
        super(SupervisedDataset, self).__init__()
        logging.warning("Loading data...")
        list_data_dict = utils.jload(data_path)

        logging.warning("Formatting inputs...")
        prompt_input, prompt_no_input = PROMPT_DICT["prompt_input"], PROMPT_DICT["prompt_no_input"]
        sources = [
            prompt_input.format_map(example) if example.get("input", "") != "" else prompt_no_input.format_map(example)
            for example in list_data_dict
        ]
        targets = [f"{example['output']}{tokenizer.eos_token}" for example in list_data_dict]

        logging.warning("Tokenizing inputs... This may take some time...")
        data_dict = preprocess(sources, targets, tokenizer)

        self.input_ids = data_dict["input_ids"]
        self.labels = data_dict["labels"]

    def __len__(self):
        return len(self.input_ids)

    def __getitem__(self, i) -> Dict[str, torch.Tensor]:
        return dict(input_ids=self.input_ids[i], labels=self.labels[i])


@dataclass
class DataCollatorForSupervisedDataset(object):
    """Collate examples for supervised fine-tuning."""

    tokenizer: transformers.PreTrainedTokenizer

    def __call__(self, instances: Sequence[Dict]) -> Dict[str, torch.Tensor]:
        input_ids, labels = tuple([instance[key] for instance in instances] for key in ("input_ids", "labels"))
        input_ids = torch.nn.utils.rnn.pad_sequence(
            input_ids, batch_first=True, padding_value=self.tokenizer.pad_token_id
        )
        labels = torch.nn.utils.rnn.pad_sequence(labels, batch_first=True, padding_value=IGNORE_INDEX)
        return dict(
            input_ids=input_ids,
            labels=labels,
            attention_mask=input_ids.ne(self.tokenizer.pad_token_id),
        )


def make_supervised_data_module(tokenizer: transformers.PreTrainedTokenizer, data_args) -> Dict:
    print("[func] make_supervised_data_module")
    """Make dataset and collator for supervised fine-tuning."""
    train_dataset = SupervisedDataset(tokenizer=tokenizer, data_path=data_args.data_path)
    data_collator = DataCollatorForSupervisedDataset(tokenizer=tokenizer)
    return dict(train_dataset=train_dataset, eval_dataset=None, data_collator=data_collator)


def get_modules(layer, is_falcon, is_opt, is_mpt):
    if is_falcon:
        return [
            layer.self_attention.query, 
            layer.self_attention.key, 
            layer.self_attention.value, 
            layer.self_attention.dense, 
            layer.mlp.dense_h_to_4h,
            layer.mlp.dense_4h_to_h,
        ]
    elif is_opt:
        return[
            layer.self_attn.q_proj,
            layer.self_attn.k_proj,
            layer.self_attn.v_proj,
            layer.self_attn.out_proj,
            layer.fc1,
            layer.fc2,
        ]
    elif is_mpt:
        return [
            layer.attn.Wq,
            layer.attn.Wk,
            layer.attn.Wv,
            layer.attn.out_proj,
            layer.ffn.up_proj,
            layer.ffn.down_proj,
        ]
    else:
        # Llama, vicuna, etc.
        return[
            layer.self_attn.q_proj,
            layer.self_attn.k_proj,
            layer.self_attn.v_proj,
            layer.self_attn.o_proj,
            layer.mlp.gate_proj,
            layer.mlp.up_proj,
            layer.mlp.down_proj,
        ]


def train():
    parser = transformers.HfArgumentParser((ModelArguments, DataArguments, TrainingArguments))
    model_args, data_args, training_args = parser.parse_args_into_dataclasses()

    config = transformers.LLaMAConfig()
    config.num_hidden_layers = 4
    config.hidden_states = 512
    config.intermediate_size = 2048

    run_vicuna = False
    from datautils import get_loaders
    DATASET = 'c4'
    print(DATASET)
    dataloader, testloader = get_loaders(DATASET,  model=model_args.model_name_or_path, seqlen=512)

    # for vicuna
    #run_vicuna = True
    #print("using vicuna dataset")
    #import pickle
    #with open('vicuna_data_input_ids.pkl', 'rb') as f:
    #    dataloader = pickle.load(f)

    #model = transformers.LLaMAForCausalLM(config)
    print(training_args.cache_dir)
    model = transformers.AutoModelForCausalLM.from_pretrained(
        model_args.model_name_or_path,
        cache_dir=training_args.cache_dir,
        trust_remote_code=True,
    )
    import pdb; pdb.set_trace()
    #model = model.cuda().bfloat16()
    model = model.bfloat16()
    #model = model.float()
    try:
        model.lm_head.cuda()
    except:
        pass

    is_falcon = 'falcon' in model_args.model_name_or_path
    is_opt = 'opt' in model_args.model_name_or_path
    is_mpt = 'mpt' in model_args.model_name_or_path

    if is_falcon:
        _model = model.transformer
        _layers = _model.h
    elif is_opt:
        _model = model.model.decoder
        _layers = _model.layers
    elif is_mpt:
        _model = model.transformer
        _layers = _model.blocks
    else:
        _model = model.model
        _layers = _model.layers

    _model.split(2) # split into n+1 machines
    #model.cuda()  # use it for not splitting

    optimizer = optim.SGD(model.parameters(), lr=0.01, momentum=0.9)
    

    if is_falcon:
        grads = [[0.] * 6 for _ in _layers]
    elif is_opt:
        grads = [[0.] * 6 for _ in _layers]
    elif is_mpt:
        grads = [[0.] * 6 for _ in _layers]
    else:
        grads = [[0.] * 7 for _ in _layers]

    for i, data in tqdm(enumerate(dataloader[:100])):
        #if i < 50:
        #    continue
        #import pdb; pdb.set_trace()
        if not run_vicuna:
            data = data[0]
        else:
            data = data.reshape(1, -1)
        x = data.cuda()
        outputs = model(input_ids=x, labels=x)
        loss = outputs.loss
        loss.backward()

        for i, layer in enumerate(_layers):
            for j, module in enumerate(get_modules(layer, is_falcon, is_opt, is_mpt)):
                grad = module.weight.grad
                #print(i, j, grad.norm())
                #import pdb; pdb.set_trace()
                # For norm
                #grads[i][j] += float((grad ** 2).mean())
                grads[i][j] += (grad ** 2).float().cpu()

        optimizer.zero_grad()
        #import pdb; pdb.set_trace()

    for i, layer in enumerate(_layers):
        for j, module in enumerate(get_modules(layer, is_falcon, is_opt, is_mpt)):
            module.weight.data = grads[i][j]

    #tokenizer = transformers.AutoTokenizer.from_pretrained(
    #    model_args.model_name_or_path,
    #    cache_dir=training_args.cache_dir,
    #    model_max_length=training_args.model_max_length,
    #    padding_side="right",
    #    use_fast=False,
    #)
    try:
        model.save_pretrained(f"./gradients-mpt-7b")
    except:
        print("error")


    #import pdb; pdb.set_trace()

    """
    if tokenizer.pad_token is None:
        smart_tokenizer_and_embedding_resize(
            special_tokens_dict=dict(pad_token=DEFAULT_PAD_TOKEN),
            tokenizer=tokenizer,
            model=model,
        )
    if "llama" in model_args.model_name_or_path:
        tokenizer.add_special_tokens(
            {
                "eos_token": DEFAULT_EOS_TOKEN,
                "bos_token": DEFAULT_BOS_TOKEN,
                "unk_token": DEFAULT_UNK_TOKEN,
            }
        )

    data_module = make_supervised_data_module(tokenizer=tokenizer, data_args=data_args)
    # import ipdb;ipdb.set_trace()
    trainer = Trainer(model=model, tokenizer=tokenizer, args=training_args, **data_module)
    trainer.train()
    # trainer.evaluate()
    trainer.save_state()
    safe_save_model_for_hf_trainer(trainer=trainer, output_dir=training_args.output_dir)
    """


if __name__ == "__main__":
    train()