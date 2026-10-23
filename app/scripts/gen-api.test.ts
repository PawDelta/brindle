// @vitest-environment node
import { describe, expect, it } from "vitest";
import { generate } from "./gen-api.mjs";

// Two DTOs as pydantic exports them: every field titled, a default makes a
// field optional, a nested model is a $ref. MilestoneDTO has a field that is
// itself called "title".
const milestone = {
  title: "MilestoneDTO",
  type: "object",
  properties: {
    id: { title: "Id", type: "integer" },
    title: { title: "Title", type: "string" },
    detail: { anyOf: [{ type: "string" }, { type: "null" }], default: null, title: "Detail" },
  },
  required: ["id", "title"],
};
const goal = (ref: string) => ({
  title: "GoalDTO",
  type: "object",
  properties: {
    root_id: { title: "Root Id", type: "string" },
    milestones: { type: "array", items: { $ref: ref }, default: [], title: "Milestones" },
  },
  required: ["root_id"],
});

describe("gen:api", () => {
  it.each([
    ["pydantic $defs", { $defs: { GoalDTO: goal("#/$defs/MilestoneDTO"), MilestoneDTO: milestone } }],
    [
      "OpenAPI components",
      { components: { schemas: { GoalDTO: goal("#/components/schemas/MilestoneDTO"), MilestoneDTO: milestone } } },
    ],
    // The engine's api-schema.json: each model nests the models it uses in its own $defs.
    [
      "the engine's models",
      { api_version: "1.0", routes: [], models: { GoalDTO: { ...goal("#/$defs/MilestoneDTO"), $defs: { MilestoneDTO: milestone } } } },
    ],
  ])("emits one interface per DTO from %s", async (_layout, schema) => {
    const ts = await generate(schema);
    expect(ts).toMatch(/export interface MilestoneDTO \{\s+id: number;\s+title: string;\s+detail\?: string \| null;\s+\}/);
    expect(ts).toMatch(/export interface GoalDTO \{\s+root_id: string;\s+milestones\?: MilestoneDTO\[\];\s+\}/);
    expect(ts).not.toMatch(/export type (Id|Title|Detail|RootId)\b/);
  });

  it("refuses a schema with no definitions", async () => {
    await expect(generate({ type: "object" })).rejects.toThrow(/no \$defs/);
    await expect(generate({ models: {} })).rejects.toThrow(/no \$defs/);
  });
});
