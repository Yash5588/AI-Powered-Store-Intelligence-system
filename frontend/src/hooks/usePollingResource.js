import { useEffect, useRef, useState } from "react";

export function usePollingResource(fetcher, deps, intervalMs) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const inFlightRef = useRef(false);

  useEffect(() => {
    let active = true;

    async function load() {
      if (inFlightRef.current) return;
      inFlightRef.current = true;
      const result = await fetcher();
      if (!active) {
        inFlightRef.current = false;
        return;
      }
      if (result.error) {
        setError(result.error);
      } else {
        setData(result.data);
        setError(null);
      }
      setLoading(false);
      inFlightRef.current = false;
    }

    setLoading(true);
    load();
    const timer = setInterval(load, intervalMs);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, deps);

  return { data, error, loading };
}
