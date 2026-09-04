const OPENAI_OAUTH_MODEL_SOURCE = "chatgpt-account";

function canonicalModelValue(schema, model) {
  const modelSchema = schema?.properties?.model;
  const modelValues = Array.isArray(modelSchema?.enum) ? modelSchema.enum : [];
  if (modelValues.includes(model)) {
    return model;
  }

  // Older saved metadata can contain the display label rather than the live
  // slug. Resolve it through the catalog before matching the conditional rule.
  const modelLabels = Array.isArray(modelSchema?.enumNames)
    ? modelSchema.enumNames
    : [];
  const labelIndex = modelLabels.indexOf(model);
  return labelIndex >= 0 ? modelValues[labelIndex] : model;
}

function getReasoningSchemaForModel(schema, model) {
  if (!schema || !model || !Array.isArray(schema.allOf)) {
    return undefined;
  }

  const selectedModel = canonicalModelValue(schema, model);
  const condition = schema.allOf.find(
    (candidate) =>
      candidate?.if?.properties?.enable_reasoning?.const === true &&
      candidate?.if?.properties?.model?.const === selectedModel,
  );
  return condition?.then?.properties?.reasoning_effort;
}

/**
 * RJSF can retain a resolved allOf branch when an account schema replaces the
 * pre-auth schema while the checkbox is already enabled. Put the selected
 * account's live field in the visible properties as well; the allOf branch
 * remains in place for validation and model-specific requiredness.
 */
function materializeReasoningProperty(schema, model, enabled) {
  if (
    !schema ||
    schema["x-openai-oauth-model-source"] !== OPENAI_OAUTH_MODEL_SOURCE ||
    !enabled
  ) {
    return schema;
  }

  const reasoningSchema = getReasoningSchemaForModel(schema, model);
  if (
    !reasoningSchema ||
    !Array.isArray(reasoningSchema.enum) ||
    reasoningSchema.enum.length === 0
  ) {
    return schema;
  }

  const sourceProperties = schema.properties || {};
  const properties = {};
  let inserted = false;
  Object.entries(sourceProperties).forEach(([name, property]) => {
    properties[name] = property;
    if (name === "enable_reasoning") {
      properties.reasoning_effort = reasoningSchema;
      inserted = true;
    }
  });
  if (!inserted) {
    properties.reasoning_effort = reasoningSchema;
  }

  return { ...schema, properties };
}

export {
  getReasoningSchemaForModel,
  materializeReasoningProperty,
  OPENAI_OAUTH_MODEL_SOURCE,
};
