import argparse
import asyncio
import os
from typing import Optional

from clients.shared.mep_client import MEPClient


def _default_key_path() -> str:
    return os.getenv(
        "MEP_QUICKSTART_KEY_PATH",
        os.path.join(os.path.expanduser("~"), ".mep", "mep_quickstart_provider.pem"),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quickstart_provider")
    parser.add_argument("--key-path", default=_default_key_path())
    parser.add_argument("--target", default=os.getenv("MEP_QUICKSTART_TARGET_NODE"))
    parser.add_argument("--model", default=os.getenv("MEP_QUICKSTART_MODEL", "cli-agent"))
    parser.add_argument("--compute-bounty", type=float, default=5.0)
    parser.add_argument("--chat-bounty", type=float, default=0.0)
    parser.add_argument("--data-price", type=float, default=2.0)
    parser.add_argument(
        "--compute-payload",
        default="Write a concise Python helper that retries a request 3 times.",
    )
    parser.add_argument(
        "--chat-payload",
        default="Hello from MEP quickstart. Are any providers online for a 0-bounty chat?",
    )
    parser.add_argument(
        "--data-payload",
        default="SAMPLE_DATASET: alpha=0.618,beta=1.414,gamma=2.718",
    )
    return parser


def _result_task_id(response: dict) -> Optional[str]:
    data = response.get("json", {})
    if isinstance(data, dict):
        task_id = data.get("task_id")
        if isinstance(task_id, str):
            return task_id
    return None


async def _submit_and_print(
    client: MEPClient,
    label: str,
    payload: str,
    bounty: float,
    model: Optional[str],
    target: Optional[str],
) -> Optional[str]:
    response = await client.submit_task(payload, bounty, model, target)
    task_id = _result_task_id(response)
    if response.get("status_code") != 200 or task_id is None:
        print(f"[{label}] submit failed: {response.get('json')}")
        return None
    print(f"[{label}] submitted: task_id={task_id}, bounty={bounty}")
    return task_id


async def run_quickstart(args: argparse.Namespace) -> int:
    client = MEPClient(args.key_path)
    print(f"[quickstart] HUB_URL={os.getenv('HUB_URL', 'https://mep-hub.silentcopilot.ai')}")
    print(f"[quickstart] WS_URL={os.getenv('WS_URL', 'wss://mep-hub.silentcopilot.ai')}")
    print(f"[quickstart] key_path={args.key_path}")
    register_data = await client.register()
    print(f"[quickstart] registered node_id={client.node_id}, balance={register_data.get('balance')}")

    compute_task = await _submit_and_print(
        client=client,
        label="compute",
        payload=args.compute_payload,
        bounty=float(args.compute_bounty),
        model=args.model,
        target=None,
    )
    chat_task = await _submit_and_print(
        client=client,
        label="chat",
        payload=args.chat_payload,
        bounty=float(args.chat_bounty),
        model=None,
        target=args.target,
    )
    data_task = await _submit_and_print(
        client=client,
        label="data",
        payload=args.data_payload,
        bounty=-abs(float(args.data_price)),
        model=None,
        target=None,
    )
    balance_response = await client.get_balance()
    if balance_response.get("status_code") == 200:
        print(f"[quickstart] current_balance={balance_response.get('json', {}).get('balance_seconds')}")
    else:
        print(f"[quickstart] balance lookup failed: {balance_response.get('json')}")

    submitted = [task for task in [compute_task, chat_task, data_task] if task]
    print(f"[quickstart] submitted_tasks={submitted}")
    return 0 if len(submitted) == 3 else 1


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run_quickstart(args)))


if __name__ == "__main__":
    main()
