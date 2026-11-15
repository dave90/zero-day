import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Search } from "lucide-react";
import { api } from "../api/client";
import type { ModelCatalog } from "../api/types";

const catalogKey = ["models", "codex"];

export function ModelSelect({
  value,
  onChange,
}: {
  value: string;
  onChange: (model: string) => void;
}) {
  const client = useQueryClient();
  const [search, setSearch] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState("");
  const query = useQuery({
    queryKey: catalogKey,
    queryFn: () => api<ModelCatalog>("/models?backend=codex"),
    staleTime: 60_000,
    retry: false,
  });
  const catalog = query.data;
  // This shared CLI default is normalized away by codex_model(). Do not show a
  // Claude-only identifier as an explicit Codex selection in existing drafts.
  const selected = value.toLowerCase().startsWith("claude") ? "" : value;
  const models = catalog?.models || [];
  const current = models.find((model) => model.id === selected);
  const defaultModel = models.find(
    (model) => model.id === catalog?.configured_default,
  );
  const defaultName = defaultModel?.name || catalog?.configured_default;
  const matches = models.filter((model) =>
    `${model.name} ${model.id} ${model.description}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  const missing = !!selected && !!catalog && !current;
  const error =
    refreshError || (query.error instanceof Error ? query.error.message : "");

  async function refresh() {
    setRefreshing(true);
    setRefreshError("");
    try {
      const data = await api<ModelCatalog>("/models?backend=codex&refresh=1");
      client.setQueryData(catalogKey, data);
    } catch (error) {
      setRefreshError((error as Error).message);
    } finally {
      setRefreshing(false);
    }
  }

  return (
    <div className="model-picker">
      <div className="model-picker-toolbar">
        <label className="search-field">
          <Search size={14} />
          <input
            type="search"
            aria-label="Search Codex models"
            placeholder="Search models…"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
        <button
          type="button"
          className="button"
          onClick={refresh}
          disabled={refreshing || query.isFetching}
          aria-label="Refresh Codex models"
        >
          <RefreshCw
            size={14}
            className={refreshing || query.isFetching ? "spin" : ""}
          />
          {refreshing || query.isFetching ? "Loading…" : "Refresh"}
        </button>
      </div>
      <select
        id="config-model"
        value={selected}
        disabled={query.isPending && !catalog}
        onChange={(event) => onChange(event.target.value)}
        aria-describedby="model-selection-help model-catalog-status"
      >
        <option value="">
          Codex default{defaultName ? ` (currently ${defaultName})` : ""}
        </option>
        {selected && !matches.some((model) => model.id === selected) && (
          <option value={selected}>
            {current?.name || selected}
            {missing ? " (saved selection, not in catalog)" : " (selected)"}
          </option>
        )}
        {matches.map((model) => (
          <option key={model.id} value={model.id}>
            {model.name}
            {model.name !== model.id ? ` · ${model.id}` : ""}
          </option>
        ))}
        {!matches.length && !query.isPending && (
          <option disabled>
            {search ? "No matching models" : "No models discovered"}
          </option>
        )}
      </select>
      {current?.description && (
        <p className="field-help">{current.description}</p>
      )}
      <p className="field-help" id="model-selection-help">
        {selected
          ? "Pinned for Zero and its agents. Your other Codex sessions and global settings stay unchanged."
          : "Follows your Codex default. Choose a specific model to pin Zero independently."}
      </p>
      <div
        className="model-catalog-status"
        id="model-catalog-status"
        role="status"
      >
        {error ? <p className="model-catalog-warning">{error}</p> : null}
        {catalog?.warning && (
          <p className="model-catalog-warning">{catalog.warning}</p>
        )}
        {missing && (
          <p className="model-catalog-warning">
            The saved model is not in this catalog. It has been retained; choose
            another model or refresh to check for changes.
          </p>
        )}
        {catalog?.source === "codex_app_server" && (
          <span>
            From your installed Codex catalog · refresh to check for new models.
          </span>
        )}
        {catalog?.source === "codex_cache" && (
          <span>
            Cached Codex model hints
            {catalog.cached_at
              ? ` · ${new Date(catalog.cached_at).toLocaleString()}`
              : ""}
          </span>
        )}
        {query.isPending && <span>Reading your Codex model catalog…</span>}
      </div>
    </div>
  );
}
