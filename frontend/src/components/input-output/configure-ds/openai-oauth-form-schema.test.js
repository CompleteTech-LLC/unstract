import { describe, expect, it } from "vitest";

import {
  getReasoningSchemaForModel,
  materializeReasoningProperty,
} from "./openai-oauth-form-schema.js";

const schema = {
  "x-openai-oauth-model-source": "chatgpt-account",
  properties: {
    model: {
      enum: ["gpt-5.6-luna", "gpt-5.6-sol"],
      enumNames: ["GPT-5.6-Luna", "GPT-5.6-Sol"],
    },
    enable_reasoning: { type: "boolean" },
  },
  allOf: [
    {
      if: {
        properties: {
          enable_reasoning: { const: true },
          model: { const: "gpt-5.6-luna" },
        },
      },
      then: {
        properties: {
          reasoning_effort: {
            type: "string",
            enum: ["low", "medium", "high", "xhigh", "max"],
          },
        },
      },
    },
  ],
};

describe("OpenAI OAuth form schema", () => {
  it("resolves reasoning by live model slug or display label", () => {
    expect(getReasoningSchemaForModel(schema, "gpt-5.6-luna").enum).toEqual([
      "low",
      "medium",
      "high",
      "xhigh",
      "max",
    ]);
    expect(getReasoningSchemaForModel(schema, "GPT-5.6-Luna").enum).toEqual([
      "low",
      "medium",
      "high",
      "xhigh",
      "max",
    ]);
  });

  it("materializes the selected account options after OAuth schema load", () => {
    const rendered = materializeReasoningProperty(schema, "gpt-5.6-luna", true);

    expect(Object.keys(rendered.properties)).toEqual([
      "model",
      "enable_reasoning",
      "reasoning_effort",
    ]);
    expect(rendered.properties.reasoning_effort.enum).toEqual([
      "low",
      "medium",
      "high",
      "xhigh",
      "max",
    ]);
  });

  it("does not add the field while reasoning is disabled", () => {
    expect(materializeReasoningProperty(schema, "gpt-5.6-luna", false)).toBe(
      schema,
    );
  });
});
