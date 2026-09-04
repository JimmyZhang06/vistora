"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export interface ConfirmationRequest {
  title: string;
  description: string;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "default" | "danger";
}

export function useConfirmDialog() {
  const [request, setRequest] = useState<ConfirmationRequest | null>(null);
  const resolverRef = useRef<((confirmed: boolean) => void) | null>(null);

  const close = useCallback((confirmed: boolean) => {
    resolverRef.current?.(confirmed);
    resolverRef.current = null;
    setRequest(null);
  }, []);

  const confirm = useCallback((nextRequest: ConfirmationRequest) => new Promise<boolean>((resolve) => {
    resolverRef.current?.(false);
    resolverRef.current = resolve;
    setRequest(nextRequest);
  }), []);

  useEffect(() => () => resolverRef.current?.(false), []);

  return {
    confirm,
    confirmationDialog: <ConfirmDialog request={request} onClose={close} />,
  };
}

function ConfirmDialog({ request, onClose }: {
  request: ConfirmationRequest | null;
  onClose: (confirmed: boolean) => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (request && !dialog.open) {
      returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      dialog.showModal();
      window.requestAnimationFrame(() => dialog.querySelector<HTMLButtonElement>("[data-confirm]")?.focus());
    } else if (!request && dialog.open) {
      dialog.close();
      returnFocusRef.current?.focus();
      returnFocusRef.current = null;
    }
  }, [request]);

  return (
    <dialog
      ref={dialogRef}
      className="confirm-dialog"
      aria-labelledby="confirm-dialog-title"
      aria-describedby="confirm-dialog-description"
      onCancel={(event) => { event.preventDefault(); onClose(false); }}
      onClose={() => { if (request) onClose(false); }}
    >
      {request ? (
        <div className="confirm-dialog-card" data-tone={request.tone ?? "default"}>
          <p className="eyebrow">CONFIRM ACTION</p>
          <h2 id="confirm-dialog-title">{request.title}</h2>
          <p id="confirm-dialog-description">{request.description}</p>
          <div className="button-row">
            <button className="button-ghost" type="button" onClick={() => onClose(false)}>{request.cancelLabel ?? "取消"}</button>
            <button
              className={request.tone === "danger" ? "button-danger" : "button"}
              type="button"
              data-confirm
              onClick={() => onClose(true)}
            >{request.confirmLabel ?? "确认"}</button>
          </div>
        </div>
      ) : null}
    </dialog>
  );
}
