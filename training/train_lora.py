from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch
from datasets import Dataset
from peft import LoraConfig, PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
from trl import SFTConfig, SFTTrainer

DEFAULT_TARGETS = [
    "q_proj",
    "k_proj",
    "v_proj",
    "o_proj",
    "gate_proj",
    "up_proj",
    "down_proj",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LoRA/QLoRA fine-tune for JARVIS.")
    parser.add_argument("--model", default="Qwen/Qwen2.5-0.5B-Instruct")
    parser.add_argument(
        "--dataset",
        default=str(Path(__file__).parent / "data" / "jarvis_sft.jsonl"),
    )
    parser.add_argument("--output-dir", default=r"D:\jarvis-training\outputs\qwen25-05b-lora")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--learning-rate", type=float, default=2e-4)
    parser.add_argument("--gradient-accumulation-steps", type=int, default=8)
    parser.add_argument("--resume-adapter", default="")
    parser.add_argument("--use-4bit", action="store_true")
    parser.add_argument("--no-4bit", action="store_true")
    return parser.parse_args()


def load_examples(path: Path) -> list[dict[str, object]]:
    examples: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                examples.append(json.loads(line))
    return examples


def main() -> None:
    args = parse_args()
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is not available in the training environment.")
    use_4bit = args.use_4bit and not args.no_4bit
    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    quantization_config = None
    if use_4bit:
        quantization_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        )

    model = AutoModelForCausalLM.from_pretrained(
        args.model,
        quantization_config=quantization_config,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        trust_remote_code=True,
    )
    model.config.use_cache = False
    resume_adapter = Path(args.resume_adapter) if args.resume_adapter else None
    if resume_adapter is not None:
        if not resume_adapter.exists():
            raise FileNotFoundError(f"Adapter not found: {resume_adapter}")
        model = PeftModel.from_pretrained(model, str(resume_adapter), is_trainable=True)

    examples = load_examples(Path(args.dataset))
    formatted = []
    for item in examples:
        messages = item["messages"]
        text = tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=False,
        )
        formatted.append({"text": text})
    dataset = Dataset.from_list(formatted)

    peft_config = LoraConfig(
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=DEFAULT_TARGETS,
    )
    training_args = SFTConfig(
        output_dir=args.output_dir,
        max_steps=args.max_steps,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        learning_rate=args.learning_rate,
        lr_scheduler_type="cosine",
        warmup_steps=min(5, max(0, args.max_steps // 5)),
        logging_steps=1,
        save_steps=max(1, args.max_steps),
        save_total_limit=1,
        bf16=True,
        gradient_checkpointing=True,
        max_length=args.max_length,
        packing=False,
        report_to="none",
        dataset_text_field="text",
        remove_unused_columns=False,
    )
    trainer = SFTTrainer(
        model=model,
        args=training_args,
        train_dataset=dataset,
        processing_class=tokenizer,
        peft_config=None if resume_adapter is not None else peft_config,
    )
    trainer.train()
    trainer.save_model(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)
    print(f"saved LoRA adapter to {args.output_dir}")


if __name__ == "__main__":
    main()
