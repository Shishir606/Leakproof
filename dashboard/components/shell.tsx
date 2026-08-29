import Link from "next/link";
import type { ReactNode } from "react";
import { CardIcon, GridIcon, TimelineIcon } from "./icons";

export function Logo() {
  return (
    <div className="brand" aria-label="Leakproof">
      <span className="brand-mark"><i /><i /></span>
      <span>leakproof</span>
    </div>
  );
}

export function Shell({ children, active }: { children: ReactNode; active: "overview" | "cases" | "demo" }) {
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <Logo />
        <nav aria-label="Primary navigation">
          <Link className={active === "overview" ? "nav-item active" : "nav-item"} href="/">
            <GridIcon /> <span>Scoreboard</span>
          </Link>
          <Link className={active === "cases" ? "nav-item active" : "nav-item"} href="/cases">
            <TimelineIcon /> <span>Case timeline</span>
          </Link>
          <Link className={active === "demo" ? "nav-item active" : "nav-item"} href="/demo">
            <CardIcon /> <span>Live checkout</span>
          </Link>
        </nav>
        <div className="sidebar-footer">
          <span className="live-dot" />
          <div><strong>Recovery spine</strong><small>simulation environment</small></div>
        </div>
      </aside>
      <main className="main-content">{children}</main>
    </div>
  );
}

export function EmptyState({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="empty-state">
      <span className="empty-orbit"><i /></span>
      <h2>{title}</h2>
      <p>{detail}</p>
      <code>make up &amp;&amp; make seed</code>
    </div>
  );
}
