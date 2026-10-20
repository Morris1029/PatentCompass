"""Persist public answer deltas independently of completed conversation messages."""
from sqlalchemy import select

from app.models import RunEvent, new_id


def stream_response(ctx, turn, messages, tools=None):
    from app import model_provider
    stream_id = f"{ctx.run_id}:{turn}:{new_id()}"
    ctx.emit("message.started", {"stream_id": stream_id})
    offset = 0

    def publish(text):
        nonlocal offset
        ctx.emit("message.delta", {"stream_id": stream_id, "offset": offset, "delta": text})
        offset += len(text)

    response = model_provider.stream_chat(messages, tools=tools, on_delta=publish)
    if response.get("tool_calls"):
        # A tool round can include transitional prose; it is not the saved final answer.
        ctx.emit("message.reset", {"stream_id": stream_id})
    return {**response, "_stream_id": stream_id}


def stream_for_run(session, run):
    events = session.scalars(select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.event_type.in_(
        ["message.started", "message.delta", "message.reset", "message.completed"])).order_by(RunEvent.seq)).all()
    stream = None
    for event in events:
        payload = event.payload
        stream_id = payload.get("stream_id")
        if not stream_id:
            continue
        if event.event_type == "message.started":
            stream = {"stream_id": stream_id, "content": "", "status": "streaming", "offset": 0, "seq": event.seq}
        elif event.event_type == "message.completed":
            content = payload["content"]
            stream = {"stream_id": stream_id, "content": content, "status": "completed", "offset": len(content),
                      "seq": event.seq, "message_id": payload["message_id"]}
        elif stream and stream["stream_id"] == stream_id:
            if event.event_type == "message.reset":
                stream.update(content="", offset=0, status="reset", seq=event.seq)
            else:
                if payload["offset"] == stream["offset"]:
                    stream["content"] += payload["delta"]
                    stream["offset"] = len(stream["content"])
                stream["seq"] = event.seq
    if stream and stream["status"] == "streaming" and run.status in {"failed", "cancelled"}:
        stream["status"] = run.status
    return stream
