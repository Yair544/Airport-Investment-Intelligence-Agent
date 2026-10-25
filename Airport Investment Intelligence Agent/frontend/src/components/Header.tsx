import type { ReactNode } from "react";
import { Logo } from "./Logo";

interface HeaderProps {
  username: string;
  onLogout: () => void;
  refresh: ReactNode;
}

export function Header({ username, onLogout, refresh }: HeaderProps) {
  return (
    <header className="topbar">
      <div className="brand">
        <Logo />
        <span className="brand__name">Airport Investment Intelligence</span>
      </div>
      <div className="topbar__actions">
        {refresh}
        <span className="user" title={`Signed in as ${username}`}>
          <span className="visually-hidden">Signed in as </span>
          <span className="user__name">{username}</span>
        </span>
        <button type="button" className="btn btn--ghost" onClick={onLogout}>
          Log out
        </button>
      </div>
    </header>
  );
}
