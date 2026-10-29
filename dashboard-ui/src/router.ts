import { useEffect, useState } from "react";

export interface Route {
  path: string;
  query: URLSearchParams;
}

function parse(): Route {
  const hash = window.location.hash.replace(/^#/, "") || "/";
  const [path, search = ""] = hash.split("?");
  return { path: path || "/", query: new URLSearchParams(search) };
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(parse);
  useEffect(() => {
    const onChange = () => setRoute(parse());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}

export function go(path: string, params: Record<string, string> = {}): void {
  const search = new URLSearchParams(
    Object.entries(params).filter(([, value]) => value !== ""),
  ).toString();
  window.location.hash = search ? `${path}?${search}` : path;
}

export function link(path: string, params: Record<string, string> = {}): string {
  const search = new URLSearchParams(
    Object.entries(params).filter(([, value]) => value !== ""),
  ).toString();
  return `#${path}${search ? `?${search}` : ""}`;
}
