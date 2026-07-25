import { useEffect, useRef, useState } from "react";

import { fetchUiFeedbackConfig, submitUiFeedback } from "./api";
import type {
  UiFeedbackConfig,
  UiFeedbackRequest,
  UiFeedbackSubmission,
} from "./contracts";

interface GoogleCredentialResponse {
  credential: string;
}

interface GoogleIdentityApi {
  accounts: {
    id: {
      initialize(options: {
        client_id: string;
        callback: (response: GoogleCredentialResponse) => void;
      }): void;
      renderButton(element: HTMLElement, options: Record<string, unknown>): void;
    };
  };
}

declare global {
  interface Window {
    google?: GoogleIdentityApi;
  }
}

const GOOGLE_IDENTITY_SCRIPT = "https://accounts.google.com/gsi/client";
const MAX_CONTEXT_LENGTH = 2_000;

function boundedText(value: string): string {
  return value.replace(/\s+/g, " ").trim().slice(0, MAX_CONTEXT_LENGTH);
}

function feedbackElement(node: Node | null): HTMLElement | null {
  const element = node instanceof HTMLElement ? node : node?.parentElement;
  return element?.closest<HTMLElement>("[data-feedback-id]") ?? null;
}

function createRequest(
  element: HTMLElement,
  rect: DOMRect,
  selectedText: string,
): UiFeedbackRequest {
  return {
    page_path: `${window.location.pathname}${window.location.search}`,
    feedback_id: element.dataset.feedbackId ?? "app-shell",
    selected_text: boundedText(selectedText || element.innerText),
    suggestion: "",
    rect: {
      x: Math.max(0, rect.x),
      y: Math.max(0, rect.y),
      width: Math.max(0, rect.width),
      height: Math.max(0, rect.height),
    },
    viewport: {
      width: window.innerWidth,
      height: window.innerHeight,
      scroll_x: window.scrollX,
      scroll_y: window.scrollY,
    },
  };
}

export function UiFeedbackController() {
  const [config, setConfig] = useState<UiFeedbackConfig | null>(null);
  const [idToken, setIdToken] = useState("");
  const [draft, setDraft] = useState<UiFeedbackRequest | null>(null);
  const [buttonPosition, setButtonPosition] = useState({ left: 0, top: 0 });
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const [suggestion, setSuggestion] = useState("");
  const [submission, setSubmission] = useState<UiFeedbackSubmission | null>(null);
  const [error, setError] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isSelectingArea, setIsSelectingArea] = useState(false);
  const loginButtonRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetchUiFeedbackConfig()
      .then(setConfig)
      .catch(() => setConfig({ enabled: false, google_client_id: "", git_branch: "", git_sha: "" }));
  }, []);

  useEffect(() => {
    if (!config?.enabled || !loginButtonRef.current) return;
    const initializeGoogle = () => {
      if (!window.google || !loginButtonRef.current) return;
      window.google.accounts.id.initialize({
        client_id: config.google_client_id,
        callback: (response) => setIdToken(response.credential),
      });
      window.google.accounts.id.renderButton(loginButtonRef.current, {
        theme: "outline",
        size: "medium",
        text: "signin_with",
      });
    };
    const existing = document.querySelector<HTMLScriptElement>(
      `script[src="${GOOGLE_IDENTITY_SCRIPT}"]`,
    );
    if (existing) {
      if (window.google) initializeGoogle();
      else existing.addEventListener("load", initializeGoogle, { once: true });
      return;
    }
    const script = document.createElement("script");
    script.src = GOOGLE_IDENTITY_SCRIPT;
    script.async = true;
    script.defer = true;
    script.addEventListener("load", initializeGoogle, { once: true });
    document.head.appendChild(script);
  }, [config]);

  useEffect(() => {
    if (!idToken) return;
    const captureSelection = () => {
      const selection = window.getSelection();
      const selectedText = boundedText(selection?.toString() ?? "");
      if (!selection || selection.rangeCount === 0 || !selectedText) {
        setDraft(null);
        return;
      }
      const range = selection.getRangeAt(0);
      const element = feedbackElement(range.commonAncestorContainer);
      if (!element) return;
      const rect = range.getBoundingClientRect();
      setDraft(createRequest(element, rect, selectedText));
      setButtonPosition({
        left: Math.min(window.innerWidth - 96, Math.max(12, rect.right + 8)),
        top: Math.min(window.innerHeight - 48, Math.max(12, rect.bottom + 8)),
      });
    };
    document.addEventListener("mouseup", captureSelection);
    return () => document.removeEventListener("mouseup", captureSelection);
  }, [idToken]);

  useEffect(() => {
    if (!idToken) return;
    const openFeedbackFromContextMenu = (event: MouseEvent) => {
      const target = event.target instanceof Element ? event.target : null;
      const element = target?.closest<HTMLElement>("[data-feedback-id]");
      if (!element || target?.closest(".ui-feedback-dialog")) return;
      event.preventDefault();
      const selection = window.getSelection();
      const selectedText = boundedText(selection?.toString() ?? "");
      const rangeRect = selection?.rangeCount
        ? selection.getRangeAt(0).getBoundingClientRect()
        : null;
      const rect = selectedText && rangeRect?.width
        ? rangeRect
        : element.getBoundingClientRect();
      setDraft(createRequest(element, rect, selectedText || element.innerText));
      setSuggestion("");
      setSubmission(null);
      setError("");
      setIsDialogOpen(true);
    };
    document.addEventListener("contextmenu", openFeedbackFromContextMenu);
    return () => {
      document.removeEventListener("contextmenu", openFeedbackFromContextMenu);
    };
  }, [idToken]);

  useEffect(() => {
    if (!isSelectingArea) return;
    const captureArea = (event: MouseEvent) => {
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest(".ui-feedback-admin")) return;
      const element = target?.closest<HTMLElement>("[data-feedback-id]");
      setIsSelectingArea(false);
      if (!element) return;
      event.preventDefault();
      event.stopPropagation();
      const rect = element.getBoundingClientRect();
      setDraft(createRequest(element, rect, element.innerText));
      setButtonPosition({
        left: Math.min(window.innerWidth - 96, event.clientX + 8),
        top: Math.min(window.innerHeight - 48, event.clientY + 8),
      });
    };
    document.addEventListener("click", captureArea, true);
    return () => document.removeEventListener("click", captureArea, true);
  }, [isSelectingArea]);

  if (!config?.enabled) return null;

  const openDialog = () => {
    setSuggestion("");
    setSubmission(null);
    setError("");
    setIsDialogOpen(true);
  };

  const sendFeedback = async () => {
    if (!draft) return;
    setIsSubmitting(true);
    setError("");
    try {
      const result = await submitUiFeedback(
        { ...draft, suggestion: suggestion.trim() },
        idToken,
      );
      setSubmission(result);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "页面改进请求失败。");
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <>
      <aside className="ui-feedback-admin" aria-label="管理员页面改进工具">
        {!idToken ? (
          <>
            <span>页面改进</span>
            <div ref={loginButtonRef} />
          </>
        ) : (
          <button
            type="button"
            className="ui-feedback-area-button"
            title="点击后再点击需要改进的页面区域"
            onClick={() => setIsSelectingArea(true)}
          >
            {isSelectingArea ? "请点击页面区域" : "选择区域"}
          </button>
        )}
      </aside>
      {idToken && draft && !isDialogOpen && (
        <button
          type="button"
          className="ui-feedback-floating-button"
          style={buttonPosition}
          onClick={openDialog}
        >
          改进
        </button>
      )}
      {isDialogOpen && draft && (
        <div className="ui-feedback-backdrop" role="presentation">
          <section
            className="ui-feedback-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="ui-feedback-title"
          >
            <span className="eyebrow">管理员工具</span>
            <h2 id="ui-feedback-title">改进这个页面区域</h2>
            <p className="ui-feedback-context">{draft.selected_text}</p>
            <label htmlFor="ui-feedback-suggestion">改进意见（可选）</label>
            <textarea
              id="ui-feedback-suggestion"
              rows={4}
              value={suggestion}
              onChange={(event) => setSuggestion(event.target.value)}
              placeholder="不填写时，Codex 会根据选中内容判断问题。"
            />
            {error && <p className="ui-feedback-error" role="alert">{error}</p>}
            {submission && (
              <p className="ui-feedback-success" role="status">
                已提交 {submission.feedback_id}。
                <a href={submission.actions_url} target="_blank" rel="noreferrer">
                  查看处理进度
                </a>
              </p>
            )}
            <div className="ui-feedback-actions">
              <button
                type="button"
                className="secondary-button"
                onClick={() => {
                  setIsDialogOpen(false);
                  setDraft(null);
                }}
              >
                关闭
              </button>
              {!submission && (
                <button type="button" disabled={isSubmitting} onClick={sendFeedback}>
                  {isSubmitting ? "正在提交…" : "提交改进任务"}
                </button>
              )}
            </div>
          </section>
        </div>
      )}
    </>
  );
}
