#!/usr/bin/env python3
"""Quick test script to debug GPT responses."""
import asyncio
import os
from spisdil_moder_bot.adapters.openai import GPTClient, ChatCompletionRequest

async def main():
    api_key = os.getenv("SPISDIL_OPENAI__API_KEY")
    if not api_key:
        print("Error: SPISDIL_OPENAI__API_KEY not set")
        return

    client = GPTClient(api_key=api_key)

    system_prompt = (
        "Strict moderation. Output format: single JSON only.\n"
        '{"violation":bool,"category":str,"severity":str,"action":str,"reason":str}\n'
        "No text before/after JSON. No explanations. No markdown. No reasoning."
    )

    test_messages = [
        "Куплю машину недорого, пишите в личку",
        "Хочу тебя избить за это",
        "Привет как дела?",
    ]

    for msg in test_messages:
        print(f"\n{'='*80}")
        print(f"Testing message: {msg}")
        print('='*80)

        user_payload = f"""chat_id: -1002484318182
user_id: 123456
message_id: 999
timestamp: 2025-01-23T11:30:00

Message:
{msg}"""

        request = ChatCompletionRequest(
            model="gpt-5-nano",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_payload},
            ],
            max_completion_tokens=2048,
            response_format={"type": "json_object"},
        )

        try:
            result = await client.complete(request)
            print(f"\nFinish reason: {result.finish_reason}")
            print(f"Tokens: total={result.tokens}, prompt={result.prompt_tokens}, completion={result.completion_tokens}")
            print(f"\nResponse content ({len(result.content)} chars):")
            print(result.content)
            print(f"\nFirst 200 chars: {result.content[:200]}")
        except Exception as e:
            print(f"Error: {e}")

    await client.close()

if __name__ == "__main__":
    asyncio.run(main())
