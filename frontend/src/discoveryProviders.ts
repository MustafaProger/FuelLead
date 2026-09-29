import type { DiscoveryProvider } from "./types";

export const discoveryProviderLabels: Record<DiscoveryProvider, string> = {
  checko: "Checko",
  okvedo: "Okvedo",
  dadata: "DaData",
  api_fns: "API-ФНС",
  combined: "Несколько источников",
  demo: "Демо-режим",
};
