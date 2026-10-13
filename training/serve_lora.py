from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import torch
from aiohttp import web
from peft import PeftModel
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    TextIteratorStreamer,
)

HIGH_RISK_MARKERS = (
    "删除",
    "格式化",
    "清空",
    "付款",
    "支付",
    "转账",
    "发消息",
    "发微信",
    "发邮件",
    "群发",
    "防火墙",
    "注册表",
    "管理员密码",
    "安装软件",
    "卸载",
)
HIGH_RISK_GUARD = (
    "安全规则：当用户请求删除文件、付款、发送消息、修改系统关键设置或其他高风险操作时，"
    "必须先说明需要用户明确确认，回复中必须包含“确认”二字，并且绝不能声称操作已经完成。"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve a JARVIS LoRA over an OpenAI API.")
    parser.add_argument("--model", default="Qwen/Qwen2.5-1.5B-Instruct")
    parser.add_argument("--adapter", default=r"D:\jarvis-training\outputs\qwen25-15b-lora-v4")
    parser.add_argument("--served-model-name", default="jarvis-lora")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--use-4bit", action="store_true")
    parser.add_argument("--max-new-tokens", type=int, default=512)
    return parser.parse_args()


class LocalModel:
    def __init__(
        self,
        model_name: str,
        adapter: Path,
        *,
        use_4bit: bool,
        max_new_tokens: int,
    ) -> None:
        quantization_config = None
        if use_4bit:
            quantization_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.bfloat16,
                bnb_4bit_use_double_quant=True,
            )
        self.tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
        model = AutoModelForCausalLM.from_pretrained(
            model_name,
            quantization_config=quantization_config,
            torch_dtype=torch.bfloat16,
            device_map="auto",
            trust_remote_code=True,
        )
        if adapter.exists():
            model = PeftModel.from_pretrained(model, str(adapter))
        model.eval()
        self.model = model
        self.device = next(model.parameters()).device
        self.max_new_tokens = max_new_tokens
        self.lock = asyncio.Lock()

    def _prompt(self, messages: list[dict[str, Any]]) -> str:
        normalized: list[dict[str, str]] = []
        for message in messages:
            role = str(message.get("role") or "user")
            if role == "developer":
                role = "system"
            content = message.get("content") or ""
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            if role in {"tool", "function"}:
                role = "user"
                content = f"工具结果：{content}"
            normalized.append({"role": role, "content": content})
        if _needs_high_risk_guard(normalized):
            if normalized and normalized[0]["role"] == "system":
                normalized[0]["content"] = (
                    f"{normalized[0]['content']}\n{HIGH_RISK_GUARD}"
                )
            else:
                normalized.insert(0, {"role": "system", "content": HIGH_RISK_GUARD})
        return self.tokenizer.apply_chat_template(
            normalized,
            tokenize=False,
            add_generation_prompt=True,
        )

    def _inputs(self, messages: list[dict[str, Any]]) -> dict[str, torch.Tensor]:
        return self.tokenizer(self._prompt(messages), return_tensors="pt").to(self.device)

    def generate(
        self,
        messages: list[dict[str, Any]],
        *,
        max_new_tokens: int,
        temperature: float,
        top_p: float,
    ) -> str:
        inputs = self._inputs(messages)
        generation: dict[str, Any] = {
            **inputs,
            "max_new_tokens": min(max_new_tokens, self.max_new_tokens),
            "do_sample": temperature > 0,
            "temperature": max(temperature, 0.01),
            "top_p": top_p,
            "pad_token_id": self.tokenizer.eos_token_id,
        }
        with torch.inference_mode():
            output = self.model.generate(**generation)
        return self.tokenizer.decode(
            output[0][inputs["input_ids"].shape[1] :],
            skip_special_tokens=True,
        ).strip()

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        max_new_tokens: int,
        temperature: float,
        top_p: float,
    ) -> AsyncIterator[str]:
        async with self.lock:
            inputs = self._inputs(messages)
            streamer = TextIteratorStreamer(
                self.tokenizer,
                skip_prompt=True,
                skip_special_tokens=True,
            )
            generation: dict[str, Any] = {
                **inputs,
                "max_new_tokens": min(max_new_tokens, self.max_new_tokens),
                "do_sample": temperature > 0,
                "temperature": max(temperature, 0.01),
                "top_p": top_p,
                "pad_token_id": self.tokenizer.eos_token_id,
                "streamer": streamer,
            }
            errors: list[BaseException] = []

            def run_generation() -> None:
                try:
                    with torch.inference_mode():
                        self.model.generate(**generation)
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    streamer.end()

            worker = threading.Thread(target=run_generation, daemon=True)
            worker.start()
            while True:
                chunk = await asyncio.to_thread(_next_or_none, streamer)
                if chunk is None:
                    break
                if chunk:
                    yield chunk
            await asyncio.to_thread(worker.join)
            if errors:
                raise RuntimeError("Local generation failed") from errors[0]


def _next_or_none(streamer: TextIteratorStreamer) -> str | None:
    try:
        return next(streamer)
    except StopIteration:
        return None


def _usage(prompt: str, reply: str) -> dict[str, int]:
    return {
        "prompt_tokens": max(1, len(prompt) // 3),
        "completion_tokens": max(1, len(reply) // 3),
        "total_tokens": max(2, (len(prompt) + len(reply)) // 3),
    }


def _needs_high_risk_guard(messages: list[dict[str, str]]) -> bool:
    for message in reversed(messages):
        if message["role"] != "user":
            continue
        return any(marker in message["content"] for marker in HIGH_RISK_MARKERS)
    return False


def _completion_payload(
    model_name: str,
    request_id: str,
    reply: str,
    prompt: str,
) -> dict[str, Any]:
    return {
        "id": request_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }
        ],
        "usage": _usage(prompt, reply),
    }


def _chunk_payload(model_name: str, request_id: str, content: str = "") -> dict[str, Any]:
    delta = {"content": content} if content else {}
    return {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model_name,
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }


async def stream_response(
    runtime: LocalModel,
    model_name: str,
    request_id: str,
    messages: list[dict[str, Any]],
    max_new_tokens: int,
    temperature: float,
    top_p: float,
) -> AsyncIterator[bytes]:
    yield b"data: " + json.dumps(
        _chunk_payload(model_name, request_id),
        ensure_ascii=False,
    ).encode("utf-8") + b"\n\n"
    async for chunk in runtime.stream(
        messages,
        max_new_tokens=max_new_tokens,
        temperature=temperature,
        top_p=top_p,
    ):
        data = json.dumps(
            _chunk_payload(model_name, request_id, chunk),
            ensure_ascii=False,
        )
        yield f"data: {data}\n\n".encode()
    final = _chunk_payload(model_name, request_id)
    final["choices"][0]["finish_reason"] = "stop"
    yield b"data: " + json.dumps(final, ensure_ascii=False).encode("utf-8") + b"\n\n"
    yield b"data: [DONE]\n\n"


def build_app(runtime: LocalModel, served_model_name: str) -> web.Application:
    app = web.Application()

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True, "model": served_model_name})

    async def models(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "object": "list",
                "data": [
                    {
                        "id": served_model_name,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "local",
                    }
                ],
            }
        )

    async def chat_completions(request: web.Request) -> web.StreamResponse:
        body = await request.json()
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise web.HTTPBadRequest(text="messages must be a non-empty list")
        requested_model = str(body.get("model") or served_model_name)
        request_id = f"chatcmpl-{uuid.uuid4().hex}"
        max_new_tokens = int(body.get("max_tokens") or runtime.max_new_tokens)
        temperature = float(body.get("temperature") or 0.0)
        top_p = float(body.get("top_p") or 0.95)

        if body.get("stream"):
            response = web.StreamResponse(
                status=200,
                headers={
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )
            await response.prepare(request)
            async for chunk in stream_response(
                runtime,
                requested_model,
                request_id,
                messages,
                max_new_tokens,
                temperature,
                top_p,
            ):
                await response.write(chunk)
            await response.write_eof()
            return response

        async with runtime.lock:
            reply = await asyncio.to_thread(
                runtime.generate,
                messages,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                top_p=top_p,
            )
        prompt = runtime._prompt(messages)
        return web.json_response(
            _completion_payload(requested_model, request_id, reply, prompt)
        )

    app.router.add_get("/health", health)
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/chat/completions", chat_completions)
    return app


def main() -> None:
    args = parse_args()
    runtime = LocalModel(
        args.model,
        Path(args.adapter),
        use_4bit=args.use_4bit,
        max_new_tokens=args.max_new_tokens,
    )
    web.run_app(
        build_app(runtime, args.served_model_name),
        host=args.host,
        port=args.port,
        print=lambda message: print(message, flush=True),
    )


if __name__ == "__main__":
    main()
