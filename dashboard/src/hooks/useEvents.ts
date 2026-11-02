import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { AgentEvent } from "../lib/api";

// Which cached queries a given event makes stale. Pushing the *fact that
// something changed* and letting React Query refetch keeps pagination and
// filtering in one place instead of duplicating them into the event stream.
const INVALIDATES: Record<string, string[]> = {
  "agent.status": ["accounts", "status"],
  "agent.paused": ["status", "accounts"],
  "agent.resumed": ["status", "accounts"],
  "agent.stopping": ["accounts"],
  "health.changed": ["accounts", "health"],
  "task.submitted": ["tasks", "summary"],
  "task.started": ["tasks", "activity", "status"],
  "task.finished": ["tasks", "activity", "summary", "status"],
  "task.failed": ["tasks", "activity", "summary"],
  "escalation.opened": ["escalations", "summary"],
  "conversation.takeover": ["conversations", "conversation"],
  "conversation.released": ["conversations", "conversation"],
  "conversation.message.received": ["conversations", "conversation"],
  "conversation.message.sent": ["conversations", "conversation"],
};

export type ConnectionState = "connecting" | "open" | "closed";

export function useEvents(onEvent?: (event: AgentEvent) => void) {
  const client = useQueryClient();
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const handler = useRef(onEvent);
  handler.current = onEvent;

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry: number | undefined;
    let delay = 1000;
    let closed = false;

    const connect = () => {
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${scheme}://${location.host}/api/events`);

      socket.onopen = () => {
        delay = 1000;
        setConnection("open");
      };

      socket.onmessage = (message) => {
        let event: AgentEvent;
        try {
          event = JSON.parse(message.data);
        } catch {
          return;
        }
        // The server pings every few seconds so a closing connection is
        // noticed on its side (see /api/events in dashboard_api/main.py).
        // It carries nothing for the page: dropping it here keeps it out of
        // the event buffer, the Activity feed, and the render cycle.
        if (event.type === "heartbeat") return;
        setEvents((previous) => [event, ...previous].slice(0, 200));
        handler.current?.(event);
        for (const key of INVALIDATES[event.type] ?? []) {
          client.invalidateQueries({ queryKey: [key] });
        }
      };

      socket.onclose = () => {
        setConnection("closed");
        if (closed) return;
        // The supervisor being down is a normal state while the operator
        // restarts it, so back off and keep trying rather than giving up.
        retry = window.setTimeout(connect, delay);
        delay = Math.min(delay * 2, 15000);
      };

      socket.onerror = () => socket?.close();
    };

    connect();
    return () => {
      closed = true;
      window.clearTimeout(retry);
      socket?.close();
    };
  }, [client]);

  return { connection, events };
}
