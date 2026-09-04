import { getDefaultFormState } from "@rjsf/utils";
import validator from "@rjsf/validator-ajv8";
import PropTypes from "prop-types";
import { useEffect, useMemo, useRef, useState } from "react";
import { Typography } from "@/components/ui/shims/antd-typography";

import { useAxiosPrivate } from "../../../hooks/useAxiosPrivate";
import { useExceptionHandler } from "../../../hooks/useExceptionHandler";
import useRequestUrl from "../../../hooks/useRequestUrl";
import { useAlertStore } from "../../../store/alert-store";
import { EmptyState } from "../../widgets/empty-state/EmptyState";
import { ConfigureDs } from "../configure-ds/ConfigureDs";

let transformLlmWhispererJsonSchema;
let LLMW_V2_ID;
let PLAN_TYPES;
let unstractSubscriptionPlanStore;
let llmWhipererAdapterSchema;
try {
  const schemaMod = await import(
    "../../../plugins/unstract-subscription/helper/transformLlmWhispererJsonSchema"
  );
  transformLlmWhispererJsonSchema = schemaMod.transformLlmWhispererJsonSchema;
  LLMW_V2_ID = schemaMod.LLMW_V2_ID;
  const constantsMod = await import(
    "../../../plugins/unstract-subscription/helper/constants"
  );
  PLAN_TYPES = constantsMod.PLAN_TYPES;
  unstractSubscriptionPlanStore = await import(
    "../../../plugins/store/unstract-subscription-plan-store"
  );
  llmWhipererAdapterSchema = await import(
    "../../../plugins/unstract-subscription/hooks/useLlmWhispererAdapterSchema.js"
  );
} catch {
  // Ignore if not available
}

const OPENAI_OAUTH_SOURCE_PREFIX = "openai-oauth|";
const OPENAI_OAUTH_DRAFT_FIELDS = new Set([
  "adapter_name",
  "model",
  "max_tokens",
  "max_retries",
  "timeout",
  "enable_reasoning",
  "reasoning_effort",
]);

const getOpenAIOAuthDraft = (formData) =>
  Object.fromEntries(
    Object.entries(formData || {}).filter(([fieldName]) =>
      OPENAI_OAUTH_DRAFT_FIELDS.has(fieldName),
    ),
  );

function AddSource({
  selectedSourceId,
  selectedSourceName,
  setOpen,
  type,
  isConnector,
  addNewItem,
  editItemId,
  metadata,
  selectedDocUrl,
  draftFormData,
  onDraftFormDataChange,
  onClearDraft,
}) {
  const [spec, setSpec] = useState({});
  const [formData, setFormData] = useState({});
  const initialDraftFormData = useRef(
    selectedSourceId.startsWith(OPENAI_OAUTH_SOURCE_PREFIX)
      ? getOpenAIOAuthDraft(draftFormData)
      : undefined,
  );
  const [isLoading, setIsLoading] = useState(false);
  const [oAuthProvider, setOAuthProvider] = useState("");
  const { setAlertDetails } = useAlertStore();
  const axiosPrivate = useAxiosPrivate();
  const handleException = useExceptionHandler();
  const { getUrl } = useRequestUrl();

  let transformLlmWhispererFormData;
  try {
    transformLlmWhispererFormData =
      llmWhipererAdapterSchema?.useLlmWhipererAdapterSchema()
        ?.transformLlmWhispererFormData;
  } catch {
    // Ignore if not available
  }

  let planType;
  if (unstractSubscriptionPlanStore?.useUnstractSubscriptionPlanStore) {
    planType = unstractSubscriptionPlanStore?.useUnstractSubscriptionPlanStore(
      (state) => state?.unstractSubscriptionPlan?.planType,
    );
  }

  const isLLMWPaidSchema = useMemo(() => {
    return (
      LLMW_V2_ID &&
      transformLlmWhispererJsonSchema &&
      PLAN_TYPES &&
      selectedSourceId === LLMW_V2_ID &&
      planType === PLAN_TYPES?.PAID
    );
  }, [
    LLMW_V2_ID,
    transformLlmWhispererJsonSchema,
    PLAN_TYPES,
    selectedSourceId,
    planType,
  ]);

  useEffect(() => {
    if (!isLLMWPaidSchema || !transformLlmWhispererFormData) {
      return;
    }

    const modifiedFormData = transformLlmWhispererFormData(formData);

    if (JSON.stringify(modifiedFormData) !== JSON.stringify(formData)) {
      setFormData(modifiedFormData);
    }
  }, [isLLMWPaidSchema, formData]);

  useEffect(() => {
    if (!selectedSourceId) {
      setSpec({});
      setOAuthProvider("");
      return;
    }

    let url;
    if (isConnector) {
      url = getUrl(`connector_schema/?id=${selectedSourceId}`);
    } else {
      url = getUrl(`adapter_schema/?id=${selectedSourceId}`);
    }

    const requestOptions = {
      method: "GET",
      url,
    };

    setIsLoading(true);
    axiosPrivate(requestOptions)
      .then((res) => {
        const data = res?.data;
        const jsonSchema = isLLMWPaidSchema
          ? transformLlmWhispererJsonSchema(data?.json_schema || {})
          : data?.json_schema || {};

        setSpec(jsonSchema);

        if (metadata && Object.keys(metadata).length > 0) {
          setFormData(metadata);
        } else if (
          initialDraftFormData.current &&
          Object.keys(initialDraftFormData.current).length > 0
        ) {
          setFormData(initialDraftFormData.current);
        } else {
          const defaults = getDefaultFormState(
            validator,
            jsonSchema,
            {},
            jsonSchema,
          );
          setFormData(defaults || {});
        }

        if (data?.oauth) {
          setOAuthProvider(data?.python_social_auth_backend);
        } else {
          setOAuthProvider("");
        }
      })
      .catch((err) => {
        setAlertDetails(handleException(err));
        setOpen(false);
      })
      .finally(() => {
        setIsLoading(false);
      });
  }, [selectedSourceId, isLLMWPaidSchema]);

  useEffect(() => {
    if (editItemId?.length && metadata && Object.keys(metadata)?.length) {
      setFormData(metadata);
    }
  }, [metadata]);

  useEffect(() => {
    if (
      !selectedSourceId.startsWith(OPENAI_OAUTH_SOURCE_PREFIX) ||
      editItemId?.length ||
      (metadata && Object.keys(metadata).length > 0)
    ) {
      return;
    }

    onDraftFormDataChange?.(selectedSourceId, getOpenAIOAuthDraft(formData));
  }, [editItemId, formData, metadata, onDraftFormDataChange, selectedSourceId]);

  const handleAddNewItem = (row, isEdit) => {
    if (!isEdit) {
      onClearDraft?.(selectedSourceId);
    }
    addNewItem?.(row, isEdit);
  };

  if (selectedSourceId.includes("pcs|")) {
    return (
      <Typography.Text>
        Edit is not supported for this connector
      </Typography.Text>
    );
  }

  if (!spec || !Object.keys(spec)?.length) {
    return <EmptyState text="Failed to load the settings form" />;
  }

  return (
    <ConfigureDs
      spec={spec}
      formData={formData}
      setFormData={setFormData}
      setOpen={setOpen}
      oAuthProvider={oAuthProvider}
      selectedSourceId={selectedSourceId}
      isLoading={isLoading}
      addNewItem={handleAddNewItem}
      type={type}
      editItemId={editItemId}
      isConnector={isConnector}
      metadata={metadata}
      selectedSourceName={selectedSourceName}
      selectedDocUrl={selectedDocUrl}
    />
  );
}

AddSource.propTypes = {
  selectedSourceId: PropTypes.string.isRequired,
  selectedSourceName: PropTypes.string,
  setOpen: PropTypes.func,
  type: PropTypes.string,
  isConnector: PropTypes.bool.isRequired,
  addNewItem: PropTypes.func,
  editItemId: PropTypes.string,
  metadata: PropTypes.object,
  selectedDocUrl: PropTypes.string,
  draftFormData: PropTypes.object,
  onDraftFormDataChange: PropTypes.func,
  onClearDraft: PropTypes.func,
};

export { AddSource };
