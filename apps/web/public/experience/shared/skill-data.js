export const skills = [
  {
    id: "historical-figure-analysis",
    owned: true,
    official: true,
    format: "9:16",
    durationMinutes: [3, 5],
    image: null,
    titleKey: "skills.history.title",
    descriptionKey: "skills.history.description",
    metaKey: "skills.history.meta",
    actionKey: "skills.history.action",
    imageBriefKey: "skills.history.imageBrief",
  },
];

export function ownedSkills(items = skills) {
  return items.filter((skill) => skill.owned === true);
}
