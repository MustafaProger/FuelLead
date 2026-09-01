export type CompanyStatus =
  | "new"
  | "checked"
  | "ready"
  | "sent"
  | "answered"
  | "interested"
  | "rejected"
  | "error";

export interface EmailAddress {
  id: number;
  email: string;
  source: string;
}

export type ContactType = "phone" | "whatsapp" | "telegram";

export interface CompanyContact {
  id: number;
  contact_type: ContactType;
  value: string;
  source: string;
  href: string;
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
  contacts: CompanyContact[];
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

export type DiscoveryProvider = "checko" | "api_fns" | "combined" | "demo";

export interface Health {
  status: string;
  app: string;
  checko_configured: boolean;
  checko_api_key_count: number;
  checko_state: "selected" | "standby" | "not_configured";
  api_fns_configured: boolean;
  api_fns_request_budget_per_run: { search: number; egr: number };
  selected_discovery_provider: DiscoveryProvider;
  mode: DiscoveryProvider;
  default_okved_codes: string[];
  target_region_codes: string[];
  discovery_limit_per_code: number;
  outreach_sender_email: string;
  gmail_auth_mode: "oauth2";
  gmail_oauth_configured: boolean;
  outreach_policy: OutreachPolicy;
}

export interface SearchRun {
  id: number;
  status: "pending" | "running" | "completed" | "failed";
  mode: DiscoveryProvider;
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
  emailProvider: "" | "yandex" | "google" | "mail_ru" | "rambler" | "other";
  category: string;
  discoveredOn: string;
  search: string;
}

export interface DashboardMetrics {
  total: number;
  with_email: number;
  ready: number;
  sent_emails: number;
  interested: number;
}

export interface DailyDiscovery {
  date: string;
  count: number;
}

export interface DashboardResponse {
  metrics: DashboardMetrics;
  status_counts: Record<CompanyStatus, number>;
  daily_discoveries: DailyDiscovery[];
  recent_companies: Company[];
}

export interface TemplateVariable {
  key: string;
  token: string;
  label: string;
}

export interface EmailTemplate {
  id: number;
  name: string;
  subject_template: string;
  body_template: string;
  updated_at: string;
  variables: TemplateVariable[];
}

export interface EmailPreview {
  company_id: number;
  company_name: string;
  recipient: string;
  subject: string;
  body: string;
}

export interface EmailSendResult {
  message_id: string;
  message_ids: string[];
  company_id: number;
  recipient: string;
  recipients: string[];
  sent_count: number;
  status: "sent";
  sent_at: string;
}

export interface OutreachPolicy {
  campaign_limit?: number;
  daily_limit: number;
  hourly_limit: number;
  min_interval_seconds: number;
  max_per_domain_per_day: number;
  eligible_status?: "ready";
  primary_address_only?: boolean;
  automatic_stop_on_provider_error?: boolean;
  opt_out_footer_enabled?: boolean;
}

export interface OutreachPreflight {
  matched_count: number;
  eligible_count: number;
  selected_count: number;
  deferred_by_campaign_limit: number;
  skipped: {
    not_ready: number;
    inactive: number;
    without_email: number;
    already_contacted: number;
    duplicate_address: number;
  };
  sender_email: string;
  gmail_configured: boolean;
  policy: OutreachPolicy;
  sample: {
    company_name: string;
    recipient: string;
    subject: string;
    body: string;
  } | null;
}

export interface OutreachCampaign {
  id: number;
  status: "running" | "paused" | "completed" | "cancelled";
  matched_count: number;
  recipient_count: number;
  sent_count: number;
  failed_count: number;
  cancelled_count: number;
  remaining_count: number;
  progress_percent: number;
  pause_reason: string | null;
  next_send_at: string | null;
  last_sent_at: string | null;
  started_at: string;
  completed_at: string | null;
  created_at: string;
  policy: OutreachPolicy;
}
