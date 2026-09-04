import PropTypes from "prop-types";
import { Button } from "@/components/ui/shims/antd-button";
import { Typography } from "@/components/ui/shims/antd-typography";

import "./OpenAIOAuthButton.css";

const OpenAIOAuthButton = ({
  handleOAuth,
  status,
  buttonText = "Sign in with OpenAI",
  disabled = false,
  verificationUrl,
  userCode,
  accountLabel,
  isStarting = false,
  isRestoring = false,
}) => {
  const isPending = status === "pending";
  const buttonLabel = isRestoring
    ? "Restoring OpenAI authentication"
    : isStarting
    ? "Starting OpenAI sign-in"
    : status === "success"
      ? "Authenticated"
      : isPending && verificationUrl
        ? "Open OpenAI device login"
        : isPending
          ? "Retry OpenAI sign-in"
          : buttonText;

  return (
    <div className="openai-oauth-layout">
      <Button
        type="primary"
        onClick={handleOAuth}
        disabled={disabled || isStarting || isRestoring}
        loading={isStarting || isRestoring}
      >
        {buttonLabel}
      </Button>
      {status === "pending" && verificationUrl && userCode && (
        <Typography.Paragraph className="openai-oauth-instructions">
          Waiting for authorization. {" "}
          Open{" "}
          <a href={verificationUrl} target="_blank" rel="noreferrer">
            OpenAI device login
          </a>{" "}
          and enter code <strong>{userCode}</strong>.
        </Typography.Paragraph>
      )}
      {accountLabel && (
        <Typography.Text type="secondary" className="openai-oauth-account">
          {accountLabel}
        </Typography.Text>
      )}
    </div>
  );
};

OpenAIOAuthButton.propTypes = {
  handleOAuth: PropTypes.func.isRequired,
  status: PropTypes.string,
  buttonText: PropTypes.string,
  disabled: PropTypes.bool,
  verificationUrl: PropTypes.string,
  userCode: PropTypes.string,
  accountLabel: PropTypes.string,
  isStarting: PropTypes.bool,
  isRestoring: PropTypes.bool,
};

export default OpenAIOAuthButton;
