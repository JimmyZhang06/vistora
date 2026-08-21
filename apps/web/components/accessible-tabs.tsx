"use client";

import type { KeyboardEvent } from "react";

export type TabOption<T extends string> = {
  id: T;
  label: string;
  count?: number;
};

export function AccessibleTabs<T extends string>({
  label,
  tabs,
  activeTab,
  onChange,
}: {
  label: string;
  tabs: readonly TabOption<T>[];
  activeTab: T;
  onChange: (tab: T) => void;
}) {
  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
    if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = tabs.length - 1;
    if (nextIndex === null) return;

    event.preventDefault();
    const nextTab = tabs[nextIndex];
    onChange(nextTab.id);
    const tabList = event.currentTarget.parentElement;
    tabList?.querySelectorAll<HTMLButtonElement>("[role=tab]")[nextIndex]?.focus();
  }

  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {tabs.map((tab, index) => {
        const selected = tab.id === activeTab;
        return (
          <button
            key={tab.id}
            id={`${label}-${tab.id}-tab`}
            className="tab"
            type="button"
            role="tab"
            aria-selected={selected}
            aria-controls={`${label}-${tab.id}-panel`}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(tab.id)}
            onKeyDown={(event) => handleKeyDown(event, index)}
          >
            {tab.label}{tab.count === undefined ? "" : ` ${tab.count}`}
          </button>
        );
      })}
    </div>
  );
}
