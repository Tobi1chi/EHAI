import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";

type ToastAction = { readonly label: string; readonly run: () => void };
type ToastFn = (message: string, action?: ToastAction) => void;

const ToastContext = createContext<ToastFn>(() => undefined);

export function useToast(): ToastFn {
  return useContext(ToastContext);
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toast, setToast] = useState<{ message: string; action?: ToastAction } | null>(null);
  const timer = useRef<number | undefined>(undefined);

  const show = useCallback<ToastFn>((message, action) => {
    window.clearTimeout(timer.current);
    setToast(action ? { message, action } : { message });
    timer.current = window.setTimeout(() => setToast(null), action ? 6000 : 3000);
  }, []);

  return (
    <ToastContext.Provider value={show}>
      {children}
      {toast && (
        <div className="toast" role="status">
          <span>{toast.message}</span>
          {toast.action && (
            <button
              type="button"
              className="btn small"
              onClick={() => {
                toast.action?.run();
                setToast(null);
              }}
            >
              {toast.action.label}
            </button>
          )}
        </div>
      )}
    </ToastContext.Provider>
  );
}
