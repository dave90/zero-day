import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, BASE } from "./client";
import { reduceEvent } from "./reducer";
import type { Session, Event } from "./types";
export function useSession(sid: string | null | undefined) {
  const client = useQueryClient();
  const [connected, setConnected] = useState(false);
  const [lastEventAt, setLastEventAt] = useState<number>();
  const query = useQuery({
    queryKey: ["session", sid],
    queryFn: () => api<Session>(`/sessions/${sid}/snapshot`),
    enabled: !!sid,
    refetchOnWindowFocus: true,
  });
  const ready = !!query.data;
  useEffect(() => {
    if (!sid || !ready) return;
    setLastEventAt(undefined);
    let timer: ReturnType<typeof setTimeout> | undefined;
    const snapshot = client.getQueryData<Session>(["session", sid]);
    const stream = new EventSource(
      // Replay the last event too: its timestamp distinguishes quiet long-running
      // work from fresh progress after a page reload. The reducer deduplicates it.
      BASE +
        `/sessions/${sid}/events?after=${Math.max(0, (snapshot?.snapshot.seq || 0) - 1)}`,
    );
    stream.onopen = () => setConnected(true);
    stream.onerror = () => setConnected(false);
    stream.onmessage = (message) => {
      const event: Event = JSON.parse(message.data);
      if (event.session_id !== sid) return;
      setLastEventAt(event.timestamp);
      client.setQueryData<Session>(["session", sid], (old) =>
        old ? { ...old, snapshot: reduceEvent(old.snapshot, event) } : old,
      );
      if (!timer)
        timer = setTimeout(() => {
          client.invalidateQueries({ queryKey: ["attacks"] });
          client.invalidateQueries({ queryKey: ["artifacts"] });
          client.invalidateQueries({ queryKey: ["findings"] });
          timer = undefined;
        }, 500);
    };
    return () => {
      stream.close();
      if (timer) clearTimeout(timer);
      setConnected(false);
    };
  }, [sid, ready, client]);
  return { ...query, connected, lastEventAt };
}
