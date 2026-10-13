from __future__ import annotations

import argparse
from pathlib import Path

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer

SYSTEM_PROMPT = (
    "你是 GCAI JARVIS，一名高情商、受过良好教育的私人 AI 管家。"
    "你先给结论，再补充关键信息；语气自然、简洁、可靠。"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run base or LoRA JARVIS inference.")
    parser.add_argument("--model", default="Qwen/Qwen2.5-0.5B-Instruct")
    parser.add_argument(
        "--adapter",
        default=r"D:\jarvis-training\outputs\qwen25-05b-lora",
    )
    parser.add_argument("--max-new-tokens", type=int, default=128)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        args.model,
        torch_dtype=torch.bfloat16,
        device_map="auto",
        trust_remote_code=True,
    )
    adapter = Path(args.adapter)
    if adapter.exists():
        model = PeftModel.from_pretrained(model, str(adapter))
    model.eval()

    prompts = [
        "你好，介绍一下你自己。",
        "打开微信。",
        "删除我 D 盘里的所有文件。",
        "我今天很累，不想动。",
    ]
    for prompt in prompts:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        text = tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )
        inputs = tokenizer(text, return_tensors="pt").to(model.device)
        with torch.inference_mode():
            outputs = model.generate(
                **inputs,
                max_new_tokens=args.max_new_tokens,
                do_sample=False,
            )
        reply = tokenizer.decode(
            outputs[0][inputs["input_ids"].shape[1] :],
            skip_special_tokens=True,
        )
        print(f"USER: {prompt}")
        print(f"JARVIS: {reply.strip()}")
        print("-" * 60)


if __name__ == "__main__":
    main()
