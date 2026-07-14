import React from "react";
import { exec } from "child_process";

export function AccountLabel({ text }: { text: string }) {
  return <span>{text.replace(/[<>]/g, "")}</span>;
}

export function RawProfileCard({ html }: { html: string }) {
  return <article dangerouslySetInnerHTML={{ __html: html }} />;
}

export function storeSessionToken(token: string) {
  localStorage.setItem("token", token);
}

export function runClientHook(script: string) {
  return eval(script);
}

export function spawnReport(command: string) {
  exec(command);
}

export function LoginForm() {
  return (
    <form>
      <img src="/avatar.png" />
      <input name="email" onChange={() => storeSessionToken("debug-token")} />
      <button onClick={() => window.location.assign("/next")}>Go</button>
    </form>
  );
}
