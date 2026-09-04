export type CompanyStatus =
  | "new"
  | "sent"
  | "answered"
  | "interested"
  | "customer"
  | "rejected";

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

export type DiscoveryProvider = "checko" | "okvedo" | "dadata" | "api_fns" | "combined" | "demo";

export interface Health {
  status: string;
  app: string;
  checko_configured: boolean;
  checko_api_key_count: number;
  checko_state: "selected" | "standby" | "not_configured";
  okvedo_configured: boolean;
  dadata_configured: boolean;
  discovery_provider_order: DiscoveryProvider[];
  api_fns_fallback_policy: "only_after_primary_daily_limits";
  api_fns_configured: boolean;
  api_fns_request_budget_per_run: { search: number; egr: number };
  selected_discovery_provider: DiscoveryProvider;
  mode: DiscoveryProvider;
  default_okved_codes: string[];
  target_region_codes: string[];
  discovery_limit_per_code: number;
  mail_credentials_encryption_configured: boolean;
  outreach_policy: OutreachPolicy;
}

export interface SearchRun {
  id: number;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  mode: DiscoveryProvider;
  search_scope: "full" | "batch";
  cancel_requested: boolean;
  active_provider: DiscoveryProvider | null;
  search_requests: number;
  company_requests: number;
  progress_message: string | null;
  provider_results: Partial<Record<DiscoveryProvider, string>>;
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
  new: number;
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
  acceptance_notice: string;
}

export interface OutreachPolicy {
  campaign_limit?: number;
  message_interval_seconds: [number, number];
  round_rest_minutes: [number, number];
  snapshot_ttl_seconds: number;
  eligible_status?: "new";
  primary_address_only?: boolean;
  sequential_smtp?: boolean;
  automatic_send_enabled?: boolean;
  opt_out_footer_enabled?: boolean;
  accepted_is_not_delivered?: boolean;
}

export interface OutreachPreflight {
  matched_count: number;
  eligible_count: number;
  selected_count: number;
  deferred_by_campaign_limit: number;
  skipped: {
    not_new: number;
    inactive: number;
    without_email: number;
    already_contacted: number;
    duplicate_address: number;
    suppressed: number;
  };
  sender_count: number;
  sender_emails: string[];
  mailru_configured: boolean;
  snapshot_id: number | null;
  snapshot_expires_at: string | null;
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
  status: "draft" | "running" | "paused" | "cooldown" | "interrupted" | "completed" | "stopped";
  matched_count: number;
  recipient_count: number;
  sent_count: number;
  queued_count: number;
  sending_count: number;
  accepted_count: number;
  failed_count: number;
  bounced_count: number;
  uncertain_count: number;
  suppressed_count: number;
  cancelled_count: number;
  remaining_count: number;
  progress_percent: number;
  pause_reason: string | null;
  current_round: number;
  active_sender_account_id: number | null;
  active_sender_email: string | null;
  sender_position: number;
  batch_position: number;
  current_batch_target: number;
  current_interval_seconds: number | null;
  next_send_at: string | null;
  round_rest_until: string | null;
  last_sent_at: string | null;
  started_at: string;
  completed_at: string | null;
  created_at: string;
  policy: OutreachPolicy;
  uncertain_deliveries: { id: number; recipient: string }[];
  acceptance_notice: string;
}

export type SenderVerificationStatus = "unverified" | "verified" | "failed" | "blocked" | "temporary_error";

export interface SenderAccount {
  id: number;
  provider: "mailru_smtp" | "gmail_api";
  email: string;
  display_name: string;
  smtp_host: string;
  smtp_port: number;
  imap_host: string;
  imap_port: number;
  smtp_enabled: boolean;
  imap_enabled: boolean;
  is_active: boolean;
  password_saved: boolean;
  verification_status: SenderVerificationStatus;
  verification_error: string | null;
  verification_checked_at: string | null;
  daily_limit: number;
  sent_today: number;
  successful_full_batches: number;
  current_batch_size: number;
  blocked_until_round: number | null;
  block_reason: string | null;
  last_sent_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface EmailSuppression {
  id: number;
  email: string;
  reason: string;
  source: string;
  campaign_id: number | null;
  delivery_id: number | null;
  smtp_code: string | null;
  created_at: string;
  lifted_at: string | null;
  comment: string | null;
  active: boolean;
}
