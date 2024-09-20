import copy
import dataclasses
import gc
import glob
import logging
import os
import re
import subprocess
import sys
from datetime import datetime
from typing import Dict, List, Optional

import datasets as hf_datasets
import pandas as pd
import safetensors.torch
import simple_parsing
import torch
import torch.distributed
import transformers
import wandb
import wandb.sdk
from torch.utils import data

from ultravox.data import dataset_config
from ultravox.data import datasets
from ultravox.model import data_processing
from ultravox.model import ultravox_config
from ultravox.model import ultravox_model
from ultravox.model import ultravox_pipeline
from ultravox.model import ultravox_processing
from ultravox.model import wandb_utils
from ultravox.training import config_base
from ultravox.training import ddp_utils

INPUT_EXAMPLE = {"text": "Transcribe\n<|audio|>", "audio": b"\x00\x00" * 16000}
OUTPUT_EXAMPLE = {"text": "Hello, world!"}


def fix_hyphens(arg: str):
    return re.sub(r"^--([^=]+)", lambda m: "--" + m.group(1).replace("-", "_"), arg)


def prepare_dataset(
    train_args: config_base.TrainConfig,
    interleave_dataset: dataset_config.InterleaveDataConfig | List[str],
    data_args: datasets.VoiceDatasetArgs,
    processor: ultravox_processing.UltravoxProcessor,
    train_on_inputs: bool,
    num_samples: Optional[int] = None,
    include_alt_fields: bool = False,  # whether to generate tensors for text-only input (e.g., used for KD training)
    enforce_ds_len_epoch: bool = False,
) -> datasets.SizedIterableDataset:
    if isinstance(interleave_dataset, dataset_config.InterleaveDataConfig):
        data_sets = [
            datasets.create_dataset(ds.dataset, data_args)
            for ds in interleave_dataset.datasets_with_multiplier
        ]
        multipliers = [
            ds.multiplier for ds in interleave_dataset.datasets_with_multiplier
        ]
        stop_strategy = interleave_dataset.stop_strategy
    else:
        data_sets = [
            datasets.create_dataset(ds, data_args) for ds in interleave_dataset
        ]
        stop_strategy = dataset_config.StopStrategy.LAST_EXHAUSTED
        multipliers = [1.0] * len(data_sets)

    # If we're using epochs to train, validate the dataset length is appropriate.
    using_epochs = train_args.max_steps == 0
    if using_epochs and enforce_ds_len_epoch:
        for ds in data_sets:
            assert (
                len(ds) > 1
            ), f"Dataset {ds} has length {len(ds)} which is too short for epoch training"
    interleave = datasets.InterleaveDataset(
        data_sets,
        stop_strategy=stop_strategy,
        multipliers=multipliers,
    )
    ds_with_proc = data_processing.UltravoxDataproc(
        interleave,
        processor=processor,
        train_on_inputs=train_on_inputs,
        include_alt_fields=include_alt_fields,
    )
    limited_ds = datasets.Range(ds_with_proc, num_samples=num_samples)
    return limited_ds


def main() -> None:
    # Disable parallelism to avoid deadlocks in DataLoader, apparently
    # multiple processes are forked when using multiple datasets.
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    # Log model checkpoints to W&B: we can reduce to model if storage is an issue
    os.environ["WANDB_LOG_MODEL"] = "checkpoint"
    os.environ["WANDB_PROJECT"] = "ultravox"

    args = simple_parsing.parse(
        config_class=config_base.TrainConfig,
        config_path="ultravox/training/configs/meta_config.yaml",  # base config file
        add_config_path_arg=True,
        args=[fix_hyphens(arg) for arg in sys.argv[1:]],
    )

    transformers.set_seed(args.seed)

    local_rank = int(os.environ.get("LOCAL_RANK", 0))
    is_master = local_rank == 0

    train(args)

    if args.do_eval and is_master:
        gc.collect()
        torch.cuda.empty_cache()
        evaluate(args)


def train(args: config_base.TrainConfig):
    world_size = int(os.environ.get("WORLD_SIZE", 1))
    local_rank = int(os.environ.get("LOCAL_RANK", 0))
    is_master = local_rank == 0

    if world_size > 1:
        torch.distributed.init_process_group(backend="nccl")

    # DDP blows up logging, so this is an attempt to suppress it to only logs from the master process
    logging.basicConfig(level=logging.INFO if is_master else logging.ERROR)
    # os.environ["TORCH_LOGS"] = "ERROR" if is_master else "WARNING"
    transformers.logging.set_verbosity(logging.WARNING if is_master else logging.ERROR)
    hf_datasets.logging.set_verbosity(logging.WARNING if is_master else logging.ERROR)

    logging.info("Instantiating processor...")
    text_tokenizer: transformers.PreTrainedTokenizerFast = (
        transformers.AutoTokenizer.from_pretrained(args.text_model)
    )
    text_tokenizer.padding_side = "right"
    text_tokenizer.pad_token = text_tokenizer.eos_token
    audio_processor = transformers.AutoProcessor.from_pretrained(args.audio_model)
    processor = ultravox_processing.UltravoxProcessor(audio_processor, text_tokenizer)

    # Instantiate the model and processor
    config = ultravox_config.UltravoxConfig(
        audio_model_id=args.audio_model,
        text_model_id=args.text_model,
        text_model_lora_config=args.text_model_lora_config,
        audio_model_lora_config=args.audio_model_lora_config,
    )

    logging.info("Instantiating model...")

    # Since the model downloads the language model and audio encoder weights, we want one process to finish up
    # downloading before the others start in order to avoid race conditions.
    with ddp_utils.run_on_master_first(is_master):
        model = ultravox_model.UltravoxModel(config)

    assert model.get_input_embeddings().num_embeddings == len(
        text_tokenizer
    ), f"Model and tokenizer mismatch: {model.get_input_embeddings().num_embeddings} != {len(text_tokenizer)}"

    model.language_model.config.use_cache = False
    if args.disable_layerdrop and hasattr(model.audio_tower.config, "layerdrop"):
        # layerdrop causes issues when training with DDP
        # https://github.com/huggingface/transformers/issues/17116#issuecomment-1121340890
        model.audio_tower.config.layerdrop = 0.0

    # loss_config needs to be passed separately just for model training
    if args.loss_config is not None:
        model.set_loss_config(args.loss_config)

    logging.info("Model and processor instantiated.")

    # Starting W&B. HF Trainer can also do this, but this way we can include the config.
    # Initializing sooner also means more of the stdout logs are captured by W&B.
    if "wandb" in args.report_logs_to and is_master:
        wandb.init(
            project=os.getenv("WANDB_PROJECT", "ultravox"),
            config=dataclasses.asdict(args),
            name=args.exp_name,
            dir="runs",
            tags=args.run_tags,
            save_code=True,
        )

    if args.model_load_dir:
        logging.info(f"Loading model state dict from {args.model_load_dir}")
        load_path = args.model_load_dir
        if wandb_utils.is_wandb_url(load_path):
            # Download the model from W&B. The main process should do the download while the others wait.
            with ddp_utils.run_on_master_first(is_master):
                load_path = wandb_utils.download_model_from_wandb(load_path)
        if os.path.isdir(load_path):
            load_path = os.path.join(load_path, "model*.safetensors")
        paths = glob.glob(load_path)
        assert len(paths) > 0, f"No model files found at {load_path}"
        for path in glob.glob(load_path):
            state_dict = safetensors.torch.load_file(path)
            mismatch = model.load_state_dict(state_dict, strict=False)
            if mismatch.unexpected_keys:
                raise ValueError(
                    f"Unexpected keys in state dict: {mismatch.unexpected_keys}"
                )

    model.print_trainable_parameters()

    # Move the model to GPU and enable bfloat16
    dtype = getattr(torch, args.data_type)
    device = torch.device(args.device, index=local_rank)
    logging.info(
        f"Using dtype and device (world_size): {dtype}, {device} ({world_size})"
    )
    model.to(device=device, dtype=dtype)

    # Prepare dataset, subsetting if needed
    train_dataset: data.IterableDataset
    val_datasets: Dict[str, data.IterableDataset]
    # We use multiple validation sets here so that the results are comparable even when training set changes
    # To make sure we can compare training and validation loss (e.g. for fine-tuning), we keep a special set
    # called "matchtrain" that uses the same data as the training set.
    val_sets = dict(
        # [("matchtrain", args.data_sets)]  # FIXME: see issue https://github.com/fixie-ai/ultravox/issues/58
        [(x, [x]) for x in args.val_sets]
        + [(f"text_{x}", [x]) for x in args.val_sets]
    )
    train_dataset = prepare_dataset(
        train_args=args,
        interleave_dataset=args.interleave_datasets,
        train_on_inputs=args.train_on_inputs,
        processor=processor,
        num_samples=args.num_samples,
        data_args=datasets.VoiceDatasetArgs(
            num_prompts=args.num_prompts,
            data_dir=args.data_dir,
            shuffle=args.shuffle_data,
            shuffle_seed=args.shuffle_seed,
            max_audio_duration_secs=args.max_audio_duration_secs,
            use_mds=args.mds,
            mds_batch_size=args.batch_size,
        ),
        include_alt_fields=model.loss_config.requires_alt_fields,
        enforce_ds_len_epoch=True,
    )
    if is_master:
        val_ds_args = datasets.VoiceDatasetArgs(
            num_prompts=1,
            split=datasets.DatasetSplit.VALIDATION,
            data_dir=args.data_dir,
            shuffle=False,
            max_audio_duration_secs=16,
            use_mds=args.mds,
            mds_batch_size=args.batch_size,
        )
        val_ds_args_text = copy.copy(val_ds_args)
        val_ds_args_text.include_audio = False
        val_datasets = {
            k: prepare_dataset(
                train_args=args,
                interleave_dataset=val_sets[k],
                train_on_inputs=args.train_on_inputs,
                processor=processor,
                num_samples=args.val_num_samples,
                data_args=val_ds_args_text if k.startswith("text_") else val_ds_args,
                include_alt_fields=model.loss_config.requires_alt_fields,
            )
            for k in val_sets
        }
        logging.info(
            f"Loaded {args.interleave_datasets} data sets, sample limit: {args.num_samples} (val sample limit: {args.val_num_samples})"
        )
    else:
        # When using DDP with split_batches=True, the primary process will distribute the batches to the workers
        # The point of this is to avoid unnecessary data processing/downloading in the workers.
        # When using epochs to train, emptydataset must have a length equal to the training set
        train_dataset = datasets.EmptyDataset(len(train_dataset))
        val_datasets = {k: datasets.EmptyDataset() for k in val_sets}

    # Set up the data loader
    data_collator = datasets.DataCollatorForSeq2SeqWithAudio(
        tokenizer=text_tokenizer,
        include_alt_fields=model.loss_config.requires_alt_fields,
    )

    logging.info(f"Config Params: {args}")
    trainer = transformers.Seq2SeqTrainer(
        model,
        train_dataset=train_dataset,
        eval_dataset=val_datasets,
        data_collator=data_collator,
        tokenizer=text_tokenizer,
        args=transformers.Seq2SeqTrainingArguments(
            dataloader_num_workers=args.num_workers if is_master else 0,
            output_dir=args.output_dir,
            run_name=args.exp_name,
            optim=args.optimizer,
            num_train_epochs=args.num_epochs,
            max_steps=args.max_steps,
            evaluation_strategy="steps",
            eval_steps=args.val_steps,
            save_strategy="steps",
            save_steps=args.save_steps,
            logging_first_step=True,
            logging_dir=args.logs_dir,
            logging_steps=args.logging_steps,
            # TODO (Farzad): reconsider for multi-node
            # In DDP world_size is set to num_gpus and we want process-0 to split the batches
            per_device_train_batch_size=args.batch_size * world_size,
            accelerator_config={"split_batches": True},
            gradient_accumulation_steps=args.grad_accum_steps,
            eval_accumulation_steps=args.val_accum_steps,
            # tf32=dtype == torch.float32 and device.type == "cuda",  # TODO: check for Ampere GPU not just CUDA
            ddp_find_unused_parameters=False,
            learning_rate=args.lr,
            lr_scheduler_type=args.lr_scheduler,
            warmup_steps=args.lr_warmup_steps,
            weight_decay=args.weight_decay,
            fp16=dtype == torch.float16,
            bf16=dtype == torch.bfloat16,
            use_cpu=args.device == "cpu",
            seed=args.seed + local_rank,
            report_to=args.report_logs_to,
            # torch_compile=True,
            # fsdp="full_shard auto_wrap",
            # fsdp_transformer_layer_cls_to_wrap='LlamaDecoderLayer',
        ),
    )

    if args.do_train:
        # Training loop
        logging.info("Starting training...")
        t_start = datetime.now()
        logging.info(f"train start time: {t_start}")
        if args.val_steps:
            trainer.evaluate()
        trainer.train()
        t_end = datetime.now()
        logging.info(f"train end time: {t_end}")
        logging.info(f"elapsed: {t_end - t_start}")

    if is_master:
        # Saving the model using pipeline to ensure its code is saved
        pipeline = ultravox_pipeline.UltravoxPipeline(
            model, tokenizer=text_tokenizer, device=device
        )
        pipeline.save_pretrained(args.output_dir)


def evaluate(args: config_base.TrainConfig):
    """
    Evaluate the model on the audio and text datasets.

    NOTE: This function must be run only on the primary process.
    """
    logging.info("Starting evaluation...")
    t_start = datetime.now()
    logging.info(f"eval start time: {t_start}")

    if args.text_model_lora_config and args.text_model_lora_config.r:
        logging.warn(
            "Model has unmerged LoRA config. This can lead to slower inference."
        )

    logs_dir = wandb.run.dir if wandb.run else str(args.logs_dir)

    # Run audio-based evaluations and log to W&B
    audio_metrics_df = run_oaievalset(
        log_dir=os.path.join(logs_dir, "oaieval/audio"),
        model_dir=str(args.output_dir),
        eval_set="audio-core",
        num_samples=args.eval_num_samples,
    )
    # TODO: it would be best to do trainer.log, but then we'd risk keeping parts of the model
    # in GPU memory, which could cause OOM errors.
    if wandb.run:
        wandb.run.log({"eval_audio": wandb.Table(data=audio_metrics_df)})

    if args.eval_text_only:
        # Run text-only evaluations and log to W&B
        text_metrics_df = run_oaievalset(
            log_dir=os.path.join(logs_dir, "oaieval/text"),
            model_dir=str(args.output_dir),
            eval_set="transcript-core",
            num_samples=args.eval_num_samples,
        )
        if wandb.run:
            wandb.run.log({"eval_text": wandb.Table(data=text_metrics_df)})

    t_end = datetime.now()
    logging.info(f"eval end time: {t_end}")
    logging.info(f"elapsed: {t_end - t_start}")


def run_oaievalset(
    log_dir: str, model_dir: str, eval_set: str, num_samples: Optional[int] = None
) -> pd.DataFrame:
    env = os.environ.copy()

    # num_gpus = max(1, torch.cuda.device_count())
    env["EVALS_THREADS"] = "64"

    # TODO: currently running this on a single GPU is faster than multiple GPUs :facepalm:
    env["CUDA_VISIBLE_DEVICES"] = "0"

    command = [
        "oaievalset",
        "--record_dir",
        log_dir,
        "generation/gpu/ultravox-dev",
        eval_set,
        f"--completion_args=model={model_dir}",
    ]
    if num_samples:
        command.append(f"--max_samples={num_samples}")

    # Run the evaluation set
    subprocess.run(command, check=True, env=env)

    # Extract the results from the log directory
    subprocess.run(
        [
            "python",
            "-m",
            "evals.elsuite.audio.make_table",
            "--out_dir",
            log_dir,
            "--log_dir",
            log_dir,
        ],
        check=True,
    )

    df = pd.read_csv(os.path.join(log_dir, "results.csv"))

    return df


if __name__ == "__main__":
    main()
