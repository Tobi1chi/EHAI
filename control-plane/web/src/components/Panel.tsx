import { useEffect, useRef, type ReactNode } from "react";
import { Icon } from "./Icon";

/** Side drawer on desktop, bottom sheet on phones. Escape and the scrim close it. */
export function Panel({
  title,
  subtitle,
  onClose,
  children,
  footer,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.focus();
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      previous?.focus();
    };
  }, [onClose]);

  return (
    <>
      <div className="scrim" onClick={onClose} />
      <div className="panel" role="dialog" aria-modal="true" tabIndex={-1} ref={ref}>
        <div className="panel-head">
          <div className="grow">
            {subtitle && <div className="meta">{subtitle}</div>}
            <div className="h2">{title}</div>
          </div>
          <button className="icon-btn" type="button" onClick={onClose} aria-label="关闭">
            <Icon name="close" />
          </button>
        </div>
        <div className="panel-body">{children}</div>
        {footer && <div className="panel-foot">{footer}</div>}
      </div>
    </>
  );
}
