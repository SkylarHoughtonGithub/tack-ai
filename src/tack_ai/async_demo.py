import asyncio
import time


async def slow_task(name: str, seconds: float) -> str:
    await asyncio.sleep(seconds)
    return f"{name} done"


async def main() -> None:
    start = time.perf_counter()

    # Run both tasks concurrently — total time is max(2, 3), not 2+3
    result_a, result_b = await asyncio.gather(
        slow_task("task-a", 2),
        slow_task("task-b", 3),
    )

    elapsed = time.perf_counter() - start
    print(f"{result_a}, {result_b}")
    print(f"elapsed: {elapsed:.2f}s  (sequential would be 5s)")


if __name__ == "__main__":
    asyncio.run(main())
