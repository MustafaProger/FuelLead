export type CompanyStatus = "new" | "checked" | "ready";

export interface EmailAddress {
  id: number;
  email: string;
  source: string;
}

export interface Okved {
  code: string | null;
  name: string | null;
}

export interface Company {
  id: number;
  name: string;
  inn: string;
  ogrn: string | null;
  primary_okved: Okved;
  activity_category: string;
  is_active: boolean;
  status: CompanyStatus;
  emails: EmailAddress[];
  first_discovered_at: string;
  last_checked_at: string;
  last_updated_at: string;
}

export interface HistoryEvent {
  id: number;
  event_type: string;
  description: string;
  from_status: string | null;
  to_status: string | null;
  created_at: string;
}

export interface CompanyDetail extends Company {
  additional_okveds: Okved[];
  history: HistoryEvent[];
}

export interface Stats {
  total: number;
  new: number;
  with_email: number;
  without_email: number;
}

export interface CompanyListResponse {
  items: Company[];
  total: number;
  page: number;
  page_size: number;
  stats: Stats;
}

export interface Health {
  status: string;
  app: string;
  checko_configured: boolean;
  mode: "checko" | "demo";
  default_okved_codes: string[];
  discovery_limit_per_code: number;
  outreach_sender_email: string;
  gmail_auth_mode: "oauth2";
  gmail_oauth_configured: boolean;
}

export interface SearchRun {
  id: number;
  status: "pending" | "running" | "completed" | "failed";
  mode: "checko" | "demo";
  requested_okved_codes: string[];
  candidates_found: number;
  companies_created: number;
  companies_updated: number;
  skipped_inactive: number;
  errors_count: number;
  error_message: string | null;
}

export interface Filters {
  status: "" | CompanyStatus;
  hasEmail: "" | "true" | "false";
  category: string;
  discoveredOn: string;
  search: string;
}
