import React from "react";
import {
  RawProfileCard,
  AccountLabel,
  runClientHook,
  spawnReport,
  storeSessionToken,
} from "gauntlet_fullstack/seed_frontend";

export function ReportPanel({
  html,
  token,
  script,
  command,
}: {
  html: string;
  token: string;
  script: string;
  command: string;
}) {
  storeSessionToken(token);
  runClientHook(script);
  spawnReport(command);

  return (
    <section>
      <img src="/report.png" />
      <RawProfileCard html={html} />
      <AccountLabel text={html} />
      <button onClick={() => (window.location.href = html)}>Open</button>
    </section>
  );
}
