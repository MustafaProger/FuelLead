import type {
  CompanyDetail,
  CompanyListResponse,
  CompanyStatus,
  ContactType,
  DashboardResponse,
  EmailPreview,
  EmailSendResult,
  EmailTemplate,
  EmailSuppression,
  Filters,
  Health,
  OutreachCampaign,
  OutreachPreflight,
  SearchRun,
  SenderAccount,
} from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "/api";

export interface AuthSession {
  authenticated: boolean;
  email: string;
}

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...options?.headers },
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    if (response.status === 401 && !path.startsWith("/auth/")) {
      window.dispatchEvent(new Event("fuellead:unauthorized"));
    }
    const detail = body?.detail;
    const message = typeof detail === "string"
      ? detail
      : Array.isArray(detail)
        ? detail
          .map((item) => typeof item?.msg === "string" ? item.msg.replace(/^Value error,\s*/, "") : null)
          .filter(Boolean)
          .join(". ")
        : `Ошибка запроса: ${response.status}`;
    throw new ApiError(message || `Ошибка запроса: ${response.status}`, response.status);
  }
  return response.json() as Promise<T>;
}

export function filtersToParams(filters: Filters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.status) params.set("status", filters.status);
  if (filters.hasEmail) params.set("has_email", filters.hasEmail);
  if (filters.emailProvider) params.set("email_provider", filters.emailProvider);
  if (filters.category) params.set("category", filters.category);
  if (filters.discoveredOn) params.set("discovered_on", filters.discoveredOn);
  if (filters.search.trim()) params.set("search", filters.search.trim());
  return params;
}

function filtersToPayload(filters: Filters) {
  return {
    status: filters.status || null,
    has_email: filters.hasEmail ? filters.hasEmail === "true" : null,
    email_provider: filters.emailProvider || null,
    category: filters.category || null,
    discovered_on: filters.discoveredOn || null,
    search: filters.search.trim() || null,
  };
}

export const api = {
  authSession: () => request<AuthSession>("/auth/session"),
  login: (email: string, password: string) => request<AuthSession>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  }),
  logout: () => request<{ authenticated: false }>("/auth/logout", { method: "POST" }),
  health: () => request<Health>("/health"),
  dashboard: () => request<DashboardResponse>("/dashboard"),
  companies: (filters: Filters, page: number, pageSize: number) => {
    const params = filtersToParams(filters);
    params.set("page", String(page));
    params.set("page_size", String(pageSize));
    return request<CompanyListResponse>(`/companies?${params}`);
  },
  company: (id: number) => request<CompanyDetail>(`/companies/${id}`),
  updateStatus: (id: number, status: CompanyStatus) =>
    request<CompanyDetail>(`/companies/${id}/status`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    }),
  addContact: (id: number, contactType: ContactType, value: string) =>
    request<CompanyDetail>(`/companies/${id}/contacts`, {
      method: "POST",
      body: JSON.stringify({ contact_type: contactType, value }),
    }),
  deleteContact: (id: number, contactId: number) =>
    request<CompanyDetail>(`/companies/${id}/contacts/${contactId}`, {
      method: "DELETE",
    }),
  deleteCompany: (id: number) =>
    request<{ deleted: true; excluded_from_discovery: true; id: number; inn: string; name: string }>(
      `/companies/${id}`,
      { method: "DELETE" },
    ),
  startSearch: (okvedCodes: string[], limitPerCode = 10) =>
    request<SearchRun>("/search-runs", {
      method: "POST",
      body: JSON.stringify({ okved_codes: okvedCodes, limit_per_code: limitPerCode }),
    }),
  searchRun: (id: number) => request<SearchRun>(`/search-runs/${id}`),
  emailTemplate: () => request<EmailTemplate>("/email-template"),
  saveEmailTemplate: (subjectTemplate: string, bodyTemplate: string) =>
    request<EmailTemplate>("/email-template", {
      method: "PUT",
      body: JSON.stringify({ subject_template: subjectTemplate, body_template: bodyTemplate }),
    }),
  previewEmail: (
    companyId: number,
    subjectTemplate: string,
    bodyTemplate: string,
    recipient?: string,
  ) => request<EmailPreview>("/email-template/preview", {
    method: "POST",
    body: JSON.stringify({
      company_id: companyId,
      subject_template: subjectTemplate,
      body_template: bodyTemplate,
      recipient: recipient || undefined,
    }),
  }),
  sendEmail: (companyId: number, recipient: string, subject: string, body: string) =>
    request<EmailSendResult>(`/companies/${companyId}/send-email`, {
      method: "POST",
      body: JSON.stringify({ recipient, subject, body }),
    }),
  sendTemplateEmail: (companyId: number) =>
    request<EmailSendResult>(`/companies/${companyId}/send-email`, {
      method: "POST",
      body: JSON.stringify({}),
    }),
  outreachPreflight: (filters: Filters) =>
    request<OutreachPreflight>("/outreach/preflight", {
      method: "POST",
      body: JSON.stringify({ filters: filtersToPayload(filters) }),
    }),
  activeOutreachCampaign: () =>
    request<OutreachCampaign | null>("/outreach/campaigns/active"),
  outreachCampaign: (id: number) =>
    request<OutreachCampaign>(`/outreach/campaigns/${id}`),
  startOutreachCampaign: (snapshotId: number) =>
    request<OutreachCampaign>("/outreach/campaigns", {
      method: "POST",
      body: JSON.stringify({ snapshot_id: snapshotId, confirmed: true }),
    }),
  pauseOutreachCampaign: (id: number) =>
    request<OutreachCampaign>(`/outreach/campaigns/${id}/pause`, { method: "POST" }),
  resumeOutreachCampaign: (id: number) =>
    request<OutreachCampaign>(`/outreach/campaigns/${id}/resume`, { method: "POST" }),
  cancelOutreachCampaign: (id: number) =>
    request<OutreachCampaign>(`/outreach/campaigns/${id}/stop`, { method: "POST" }),
  resolveUncertainDelivery: (id: number, outcome: "accepted" | "failed") =>
    request<OutreachCampaign>(`/outreach/deliveries/${id}/resolve-uncertain`, {
      method: "POST",
      body: JSON.stringify({ outcome, confirmed: true }),
    }),
  senderAccounts: () => request<SenderAccount[]>("/sender-accounts"),
  createSenderAccount: (data: {
    email: string;
    display_name: string;
    password: string;
    daily_limit: number;
    smtp_enabled: boolean;
    imap_enabled: boolean;
  }) => request<SenderAccount>("/sender-accounts", {
    method: "POST",
    body: JSON.stringify({ provider: "mailru_smtp", ...data }),
  }),
  updateSenderAccount: (id: number, data: Partial<{
    display_name: string;
    password: string;
    daily_limit: number;
    smtp_enabled: boolean;
    imap_enabled: boolean;
    is_active: boolean;
  }>) => request<SenderAccount>(`/sender-accounts/${id}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  }),
  verifySenderAccount: (id: number) =>
    request<SenderAccount>(`/sender-accounts/${id}/verify`, { method: "POST" }),
  sendSenderTestEmail: (id: number, recipient: string) =>
    request<{ accepted: true; recipient: string; message_id: string; notice: string }>(
      `/sender-accounts/${id}/test-email`,
      { method: "POST", body: JSON.stringify({ recipient, confirmed: true }) },
    ),
  deleteSenderAccount: (id: number) =>
    request<{ deleted: true; id: number }>(`/sender-accounts/${id}?confirmed=true`, { method: "DELETE" }),
  emailSuppressions: (search = "") =>
    request<EmailSuppression[]>(`/email-suppressions?search=${encodeURIComponent(search)}`),
  addEmailSuppression: (email: string, reason: string, comment: string) =>
    request<EmailSuppression>("/email-suppressions", {
      method: "POST",
      body: JSON.stringify({ email, reason, comment: comment || null }),
    }),
  liftEmailSuppression: (id: number, comment: string) =>
    request<EmailSuppression>(`/email-suppressions/${id}/lift`, {
      method: "POST",
      body: JSON.stringify({ confirmed: true, comment }),
    }),
  exportUrl: (filters: Filters) => `${API_BASE}/export.xlsx?${filtersToParams(filters)}`,
};
