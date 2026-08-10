import { useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

interface TermHelpProps {
  label: string;
  description: string;
}

export function TermHelp({ label, description }: TermHelpProps) {
  const anchorRef = useRef<HTMLSpanElement>(null);
  const tooltipRef = useRef<HTMLSpanElement>(null);
  const tooltipId = useId();
  const [isVisible, setIsVisible] = useState(false);
  const [position, setPosition] = useState({ left: 0, top: 0, placeAbove: false });

  useLayoutEffect(() => {
    if (!isVisible) return;

    const updatePosition = () => {
      const anchor = anchorRef.current?.getBoundingClientRect();
      const tooltip = tooltipRef.current?.getBoundingClientRect();
      if (!anchor || !tooltip) return;

      const viewportPadding = 12;
      const tooltipGap = 8;
      const idealLeft = anchor.left + anchor.width / 2;
      const halfWidth = tooltip.width / 2;
      const left = Math.min(
        window.innerWidth - viewportPadding - halfWidth,
        Math.max(viewportPadding + halfWidth, idealLeft),
      );
      const placeAbove = anchor.bottom + tooltipGap + tooltip.height > window.innerHeight - viewportPadding
        && anchor.top - tooltipGap - tooltip.height >= viewportPadding;

      setPosition({
        left,
        top: placeAbove ? anchor.top - tooltipGap : anchor.bottom + tooltipGap,
        placeAbove,
      });
    };

    updatePosition();
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);
    return () => {
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
    };
  }, [isVisible]);

  return (
    <span
      ref={anchorRef}
      className="term-help"
      tabIndex={0}
      aria-label={`${label}：${description}`}
      aria-describedby={isVisible ? tooltipId : undefined}
      onMouseEnter={() => setIsVisible(true)}
      onMouseLeave={() => setIsVisible(false)}
      onFocus={() => setIsVisible(true)}
      onBlur={() => setIsVisible(false)}
    >
      <span aria-hidden="true">!</span>
      {isVisible && createPortal(
        <span
          ref={tooltipRef}
          id={tooltipId}
          className="term-help-content"
          role="tooltip"
          style={{
            left: position.left,
            top: position.top,
            transform: position.placeAbove
              ? "translate(-50%, -100%)"
              : "translateX(-50%)",
          }}
        >
          {description}
        </span>,
        document.body,
      )}
    </span>
  );
}
