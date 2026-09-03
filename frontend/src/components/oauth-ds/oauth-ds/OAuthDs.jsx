import PropTypes from "prop-types";
import { useCallback, useEffect, useState } from "react";
import { Typography } from "@/components/ui/shims/antd-typography";

import { getBaseUrl, O_AUTH_PROVIDERS } from "../../../helpers/GetStaticData";
import { useAxiosPrivate } from "../../../hooks/useAxiosPrivate.js";
import { useExceptionHandler } from "../../../hooks/useExceptionHandler.jsx";
import { useAlertStore } from "../../../store/alert-store";
import GoogleOAuthButton from "../google/GoogleOAuthButton.jsx";
import MicrosoftOAuthButton from "../microsoft/MicrosoftOAuthButton.jsx";
import OpenAIOAuthButton from "../openai/OpenAIOAuthButton.jsx";

function OAuthDs({
  oAuthProvider,
  setCacheKey,
  setStatus,
  selectedSourceId,
  isExistingConnector,
  disabled = false,
}) {
  const axiosPrivate = useAxiosPrivate();
  const { setAlertDetails } = useAlertStore();
  const handleException = useExceptionHandler();

  // Simple OAuth storage keys per connector
  const oauthCacheKey = `oauth-cachekey-${selectedSourceId}`;
  const oauthStatusKey = `oauth-status-${selectedSourceId}`;

  // Determine button text based on connector state and provider
  const getButtonText = () => {
    if (isExistingConnector) {
      return "Reauthenticate";
    }
    if (oAuthProvider === O_AUTH_PROVIDERS.MICROSOFT) {
      return "Sign in with Microsoft";
    }
    if (oAuthProvider === O_AUTH_PROVIDERS.GOOGLE) {
      return "Authenticate with Google";
    }
    if (oAuthProvider === O_AUTH_PROVIDERS.OPENAI) {
      return "Sign in with OpenAI";
    }
    return "Authenticate";
  };

  const buttonText = getButtonText();

  const [oauthStatus, setOAuthStatus] = useState(() => {
    // Initialize from connector-specific status
    return localStorage.getItem(oauthStatusKey);
  });
  const [loginCacheKey, setLoginCacheKey] = useState(() =>
    localStorage.getItem(oauthCacheKey),
  );
  const [deviceLogin, setDeviceLogin] = useState(() => {
    try {
      return JSON.parse(localStorage.getItem(`oauth-device-${selectedSourceId}`));
    } catch {
      return null;
    }
  });

  const updateOAuthStatus = useCallback((newStatus) => {
    setOAuthStatus(newStatus);
    setStatus(newStatus);
    localStorage.setItem(oauthStatusKey, newStatus);
  }, [oauthStatusKey, setStatus]);

  useEffect(() => {
    const handleStorageChange = () => {
      // Listen for changes to our specific connector only
      const updatedOAuthStatus = localStorage.getItem(oauthStatusKey);
      if (updatedOAuthStatus) {
        setOAuthStatus(updatedOAuthStatus);
        setStatus(updatedOAuthStatus);
      }
    };

    window.addEventListener("storage", handleStorageChange);

    // Load persisted cache key if available
    const persistedCacheKey = localStorage.getItem(oauthCacheKey);
    setLoginCacheKey(persistedCacheKey || null);
    if (persistedCacheKey) {
      setCacheKey(persistedCacheKey);
    } else {
      setCacheKey("");
    }

    // Set initial status from connector-specific status
    const connectorStatus = localStorage.getItem(oauthStatusKey);
    setStatus(connectorStatus || "");
    setOAuthStatus(connectorStatus || "");

    const persistedDeviceLogin = localStorage.getItem(
      `oauth-device-${selectedSourceId}`,
    );
    if (persistedDeviceLogin) {
      try {
        setDeviceLogin(JSON.parse(persistedDeviceLogin));
      } catch {
        localStorage.removeItem(`oauth-device-${selectedSourceId}`);
      }
    } else {
      setDeviceLogin(null);
    }

    return () => {
      window.removeEventListener("storage", handleStorageChange);
      // Don't clear localStorage on unmount to persist across tab switches
    };
  }, [selectedSourceId, oauthCacheKey, oauthStatusKey, setCacheKey, setStatus]);

  useEffect(() => {
    if (
      oAuthProvider !== O_AUTH_PROVIDERS.OPENAI ||
      oauthStatus !== "pending" ||
      !loginCacheKey
    ) {
      return undefined;
    }

    let isActive = true;
    const pollLogin = async () => {
      try {
        const response = await axiosPrivate({
          method: "GET",
          url: `/api/v1/oauth/openai/poll/?oauth-key=${encodeURIComponent(loginCacheKey)}`,
        });
        if (!isActive) {
          return;
        }
        const result = response?.data || {};
        if (result.status === "success") {
          setDeviceLogin((current) => ({
            ...(current || {}),
            account_label: result.account_label,
          }));
          updateOAuthStatus("success");
        }
      } catch (err) {
        if (!isActive) {
          return;
        }
        const message =
          err?.response?.data?.message || "OpenAI authentication failed";
        updateOAuthStatus("error");
        setAlertDetails(handleException(err, message));
      }
    };

    const initialPoll = setTimeout(pollLogin, 1000);
    const pollInterval = setInterval(pollLogin, 5000);
    return () => {
      isActive = false;
      clearTimeout(initialPoll);
      clearInterval(pollInterval);
    };
  }, [
    axiosPrivate,
    handleException,
    loginCacheKey,
    oauthStatus,
    oAuthProvider,
    setAlertDetails,
    updateOAuthStatus,
  ]);

  const handleOAuth = async () => {
    try {
      if (oAuthProvider === O_AUTH_PROVIDERS.OPENAI) {
        const response = await axiosPrivate({
          method: "POST",
          url: "/api/v1/oauth/openai/start/",
        });
        const loginDetails = response?.data || {};
        const newCacheKey = loginDetails.cache_key;
        if (!newCacheKey) {
          throw new Error("OpenAI OAuth did not return a login session");
        }
        setLoginCacheKey(newCacheKey);
        setCacheKey(newCacheKey);
        localStorage.setItem(oauthCacheKey, newCacheKey);
        setDeviceLogin(loginDetails);
        localStorage.setItem(
          `oauth-device-${selectedSourceId}`,
          JSON.stringify(loginDetails),
        );
        updateOAuthStatus("pending");
        return;
      }

      // Store connector context in sessionStorage for OAuth callback (survives window.open)
      sessionStorage.setItem("oauth-current-connector", selectedSourceId);

      const requestOptions = {
        method: "GET",
        url: `/api/v1/oauth/cache-key/${oAuthProvider}`,
      };

      const response = await axiosPrivate(requestOptions);
      const cacheKey = response?.data?.cache_key;
      const encodedCacheKey = encodeURIComponent(cacheKey);
      setCacheKey(cacheKey);

      // Persist cache key to localStorage
      localStorage.setItem(oauthCacheKey, cacheKey);

      const baseUrl = getBaseUrl();

      const url = `${baseUrl}/api/v1/oauth/login/${oAuthProvider}?oauth-key=${encodedCacheKey}`;

      // Open in a new window
      window.open(
        url,
        "_blank",
        "toolbar=yes,scrollbars=yes,resizable=yes,top=200,left=500,width=500,height=600",
      );
    } catch (err) {
      setAlertDetails(handleException(err));
    }
  };

  if (O_AUTH_PROVIDERS.GOOGLE === oAuthProvider) {
    return (
      <>
        <GoogleOAuthButton
          handleOAuth={handleOAuth}
          status={oauthStatus}
          buttonText={buttonText}
          disabled={disabled}
        />
      </>
    );
  }

  if (O_AUTH_PROVIDERS.MICROSOFT === oAuthProvider) {
    return (
      <>
        <MicrosoftOAuthButton
          handleOAuth={handleOAuth}
          status={oauthStatus}
          buttonText={buttonText}
          disabled={disabled}
        />
      </>
    );
  }

  if (O_AUTH_PROVIDERS.OPENAI === oAuthProvider) {
    return (
      <OpenAIOAuthButton
        handleOAuth={handleOAuth}
        status={oauthStatus}
        buttonText={buttonText}
        disabled={disabled}
        verificationUrl={deviceLogin?.verification_url}
        userCode={deviceLogin?.user_code}
        accountLabel={deviceLogin?.account_label}
      />
    );
  }

  return <Typography>Provider not available.</Typography>;
}

OAuthDs.propTypes = {
  oAuthProvider: PropTypes.string,
  setCacheKey: PropTypes.func,
  setStatus: PropTypes.func,
  selectedSourceId: PropTypes.string.isRequired,
  isExistingConnector: PropTypes.bool,
  disabled: PropTypes.bool,
};

export { OAuthDs };
