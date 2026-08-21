"use client";

import {
  Children,
  isValidElement,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";

type OptionProps = {
  children?: ReactNode;
  disabled?: boolean;
  value?: number | string;
};

type SelectOption = {
  disabled: boolean;
  label: ReactNode;
  text: string;
  value: string;
};

type UiSelectProps = {
  ariaLabel: string;
  children: ReactNode;
  className?: string;
  disabled?: boolean;
  onChange: (value: string) => void;
  value: string;
};

function textValue(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  return Children.toArray(node).map(textValue).join("");
}

export function UiSelect({
  ariaLabel,
  children,
  className = "",
  disabled = false,
  onChange,
  value,
}: UiSelectProps) {
  const listboxId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);

  const options = useMemo<SelectOption[]>(() => (
    Children.toArray(children).flatMap((child) => {
      if (!isValidElement<OptionProps>(child)) return [];
      const label = child.props.children;
      return [{
        disabled: Boolean(child.props.disabled),
        label,
        text: textValue(label),
        value: String(child.props.value ?? ""),
      }];
    })
  ), [children]);

  const selectedIndex = Math.max(0, options.findIndex((option) => option.value === value));
  const selected = options[selectedIndex];

  useEffect(() => {
    if (!open) return;
    function closeOnOutsidePointer(event: PointerEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [open]);

  function focusOption(index: number) {
    const next = options[index];
    if (!next || next.disabled) return;
    setActiveIndex(index);
    window.requestAnimationFrame(() => optionRefs.current[index]?.focus());
  }

  function openMenu(index = selectedIndex) {
    if (disabled) return;
    setOpen(true);
    focusOption(index);
  }

  function moveActive(direction: 1 | -1) {
    if (!options.length) return;
    let index = activeIndex;
    for (let count = 0; count < options.length; count += 1) {
      index = (index + direction + options.length) % options.length;
      if (!options[index]?.disabled) {
        focusOption(index);
        return;
      }
    }
  }

  function handleTriggerKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      openMenu(selectedIndex);
    }
  }

  function handleOptionKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      moveActive(1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      moveActive(-1);
    } else if (event.key === "Home") {
      event.preventDefault();
      focusOption(options.findIndex((option) => !option.disabled));
    } else if (event.key === "End") {
      event.preventDefault();
      focusOption(options.findLastIndex((option) => !option.disabled));
    } else if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
    } else if (event.key === "Tab") {
      setOpen(false);
    } else if (event.key.length === 1) {
      const query = event.key.toLocaleLowerCase();
      const match = options.findIndex((option, index) => (
        index !== activeIndex
        && !option.disabled
        && option.text.toLocaleLowerCase().startsWith(query)
      ));
      if (match >= 0) focusOption(match);
    }
  }

  return (
    <div ref={rootRef} className={`ui-select ${className}`.trim()} data-open={open || undefined}>
      <button
        ref={triggerRef}
        className="select ui-select-trigger"
        type="button"
        role="combobox"
        aria-label={ariaLabel}
        aria-controls={listboxId}
        aria-expanded={open}
        aria-haspopup="listbox"
        disabled={disabled}
        onClick={() => open ? setOpen(false) : openMenu()}
        onKeyDown={handleTriggerKeyDown}
      >
        <span>{selected?.label ?? "请选择"}</span>
        <i aria-hidden="true" />
      </button>

      {open ? (
        <div className="ui-select-popover">
          <div id={listboxId} className="ui-select-list" role="listbox" aria-label={ariaLabel}>
            {options.map((option, index) => (
              <button
                key={`${option.value}:${index}`}
                ref={(node) => { optionRefs.current[index] = node; }}
                className="ui-select-option"
                type="button"
                role="option"
                aria-selected={option.value === value}
                disabled={option.disabled}
                onClick={() => {
                  onChange(option.value);
                  setOpen(false);
                  triggerRef.current?.focus();
                }}
                onFocus={() => setActiveIndex(index)}
                onKeyDown={handleOptionKeyDown}
              >
                <span className="ui-select-check" aria-hidden="true">{option.value === value ? "✓" : ""}</span>
                <span>{option.label}</span>
              </button>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
}
