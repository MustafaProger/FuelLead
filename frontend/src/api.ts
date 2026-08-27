import type {
  CompanyDetail,
  CompanyListResponse,
  CompanyStatus,
  Filters,
  Health,
  SearchRun,
} from "./types";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "/api";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...options?.headers },
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(body?.detail || `Ошибка запроса: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export function filtersToParams(filters: Filters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.status) params.set("status", filters.status);
  if (filters.hasEmail) params.set("has_email", filters.hasEmail);
  if (filters.category) params.set("category", filters.category);
  if (filters.discoveredOn) params.set("discovered_on", filters.discoveredOn);
  if (filters.search.trim()) params.set("search", filters.search.trim());
  return params;
}

export const api = {
  health: () => request<Health>("/health"),
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
  startSearch: (okvedCodes: string[], limitPerCode = 10) =>
    request<SearchRun>("/search-runs", {
      method: "POST",
      body: JSON.stringify({ okved_codes: okvedCodes, limit_per_code: limitPerCode }),
    }),
  searchRun: (id: number) => request<SearchRun>(`/search-runs/${id}`),
  exportUrl: (filters: Filters) => `${API_BASE}/export.xlsx?${filtersToParams(filters)}`,
};

