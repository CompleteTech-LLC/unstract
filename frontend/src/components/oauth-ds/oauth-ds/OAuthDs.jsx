import PropTypes from "prop-types";
import { useCallback, useEffect, useState } from "react";
import Cookies from "js-cookie";
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
  hasOAuthCredentials = false,
  oauthAccountLabel,
  adapterInstanceId,
  onModelsLoaded,
  disabled = false,
}) {
  const axiosPrivate = useAxiosPrivate();
  const { setAlertDetails } = useAlertStore();
  const handleException = useExceptionHandler();

  // Keep transient OAuth hand-off state isolated per saved adapter. New
  // adapters use the provider id until they are saved; saved adapters use
  // their instance id so multiple ChatGPT accounts never share browser state.
  const oauthStateScope = adapterInstanceId || selectedSourceId;
  const oauthCacheKey = `oauth-cachekey-${oauthStateScope}`;
  const oauthStatusKey = `oauth-status-${oauthStateScope}`;
  const oauthDeviceKey = `oauth-device-${oauthStateScope}`;

  // Determine button text based on connector state and provider
  const getButtonText = () => {
    if (
      oAuthProvider === O_AUTH_PROVIDERS.OPENAI &&
      hasOAuthCredentials
    ) {
      return "Authenticated";
    }
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
    // A durable OAuth adapter is authenticated even when there is no
    // browser-side hand-off session left to restore.
    return hasOAuthCredentials
      ? "success"
      : localStorage.getItem(oauthStatusKey);
  });
  const [loginCacheKey, setLoginCacheKey] = useState(() =>
    localStorage.getItem(oauthCacheKey),
  );
  const [deviceLogin, setDeviceLogin] = useState(() => {
    try {
      return JSON.parse(localStorage.getItem(oauthDeviceKey));
    } catch {
      return null;
    }
  });
  const [activeLoginCacheKey, setActiveLoginCacheKey] = useState(null);
  const [isStarting, setIsStarting] = useState(false);

  const updateOAuthStatus = useCallback((newStatus) => {
    setOAuthStatus(newStatus);
    setStatus(newStatus);
    localStorage.setItem(oauthStatusKey, newStatus);
  }, [oauthStatusKey, setStatus]);

  const loadOpenAIModelSchema = useCallback(
    async (oauthKey = "") => {
      const params = new URLSearchParams();
      if (oauthKey) {
        params.set("oauth-key", oauthKey);
      } else if (adapterInstanceId) {
        params.set("adapter-instance-id", adapterInstanceId);
      } else {
        return;
      }

      const response = await axiosPrivate({
        method: "GET",
        url: `/api/v1/oauth/openai/models/?${params.toString()}`,
      });
      const dynamicSchema = response?.data?.json_schema;
      if (!dynamicSchema) {
        throw new Error("OpenAI OAuth returned no model schema");
      }
      onModelsLoaded?.(dynamicSchema);
    },
    [adapterInstanceId, axiosPrivate, onModelsLoaded],
  );

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

    const persistedDeviceLogin = localStorage.getItem(oauthDeviceKey);
    if (persistedDeviceLogin) {
      try {
        setDeviceLogin(JSON.parse(persistedDeviceLogin));
      } catch {
        localStorage.removeItem(oauthDeviceKey);
      }
    } else {
      setDeviceLogin(null);
    }

    return () => {
      window.removeEventListener("storage", handleStorageChange);
      // Don't clear localStorage on unmount to persist across tab switches
    };
  }, [
    oauthCacheKey,
    oauthDeviceKey,
    oauthStatusKey,
    selectedSourceId,
    setCacheKey,
    setStatus,
  ]);

  useEffect(() => {
    // Do not let an old pending browser state mask credentials already saved
    // on an existing adapter. A live reauthentication session is allowed to
    // remain pending and can still replace those credentials after testing.
    if (
      oAuthProvider !== O_AUTH_PROVIDERS.OPENAI ||
      !hasOAuthCredentials ||
      loginCacheKey ||
      activeLoginCacheKey ||
      oauthStatus === "success"
    ) {
      return;
    }
    setOAuthStatus("success");
    setStatus("success");
  }, [
    activeLoginCacheKey,
    hasOAuthCredentials,
    loginCacheKey,
    oAuthProvider,
    oauthStatus,
    setStatus,
  ]);

  useEffect(() => {
    if (oAuthProvider !== O_AUTH_PROVIDERS.OPENAI || !onModelsLoaded) {
      return undefined;
    }

    // Existing adapters use their server-side encrypted credentials. A new
    // OAuth login takes precedence only after that login has completed, so a
    // stale localStorage cache key cannot select another account by accident.
    const sessionKey =
      oauthStatus === "success"
        ? activeLoginCacheKey || (!adapterInstanceId ? loginCacheKey : "")
        : "";
    const source = sessionKey || (adapterInstanceId ? adapterInstanceId : "");
    if (!source) {
      return undefined;
    }

    let isActive = true;
    loadOpenAIModelSchema(sessionKey)
      .catch((err) => {
        if (!isActive) {
          return;
        }
        const message =
          err?.response?.data?.message ||
          "Could not load models available to this OpenAI account";
        setAlertDetails(handleException(err, message));
      });

    return () => {
      isActive = false;
    };
  }, [
    activeLoginCacheKey,
    adapterInstanceId,
    handleException,
    loadOpenAIModelSchema,
    loginCacheKey,
    oauthStatus,
    oAuthProvider,
    onModelsLoaded,
    setAlertDetails,
  ]);

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
    let loginWindow;
    try {
      if (oAuthProvider === O_AUTH_PROVIDERS.OPENAI) {
        if (oauthStatus === "pending" && deviceLogin?.verification_url) {
          window.open(
            deviceLogin.verification_url,
            "_blank",
            "toolbar=yes,scrollbars=yes,resizable=yes,top=200,left=500,width=500,height=600",
          );
          return;
        }

        setIsStarting(true);
        // Open a user-initiated window before awaiting the API call. Browsers
        // may block a window opened only after the device-code request returns;
        // the visible link below remains the fallback when that happens.
        loginWindow = window.open(
          "about:blank",
          "_blank",
          "toolbar=yes,scrollbars=yes,resizable=yes,top=200,left=500,width=500,height=600",
        );
        const response = await axiosPrivate({
          method: "POST",
          url: "/api/v1/oauth/openai/start/",
          headers: {
            "X-CSRFToken": Cookies.get("csrftoken"),
          },
        });
        const loginDetails = response?.data || {};
        const newCacheKey = loginDetails.cache_key;
        if (!newCacheKey) {
          throw new Error("OpenAI OAuth did not return a login session");
        }
        setLoginCacheKey(newCacheKey);
        setActiveLoginCacheKey(newCacheKey);
        setCacheKey(newCacheKey);
        localStorage.setItem(oauthCacheKey, newCacheKey);
        setDeviceLogin(loginDetails);
        localStorage.setItem(
          oauthDeviceKey,
          JSON.stringify(loginDetails),
        );
        updateOAuthStatus("pending");
        if (loginWindow && loginDetails.verification_url) {
          loginWindow.location.href = loginDetails.verification_url;
        } else if (loginDetails.verification_url) {
          window.open(
            loginDetails.verification_url,
            "_blank",
            "toolbar=yes,scrollbars=yes,resizable=yes,top=200,left=500,width=500,height=600",
          );
        }
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
      if (loginWindow && !loginWindow.closed) {
        loginWindow.close();
      }
      if (oAuthProvider === O_AUTH_PROVIDERS.OPENAI) {
        setIsStarting(false);
        updateOAuthStatus("error");
      }
      setAlertDetails(handleException(err));
    } finally {
      if (oAuthProvider === O_AUTH_PROVIDERS.OPENAI) {
        setIsStarting(false);
      }
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
        accountLabel={deviceLogin?.account_label || oauthAccountLabel}
        isStarting={isStarting}
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
  hasOAuthCredentials: PropTypes.bool,
  oauthAccountLabel: PropTypes.string,
  adapterInstanceId: PropTypes.string,
  onModelsLoaded: PropTypes.func,
  disabled: PropTypes.bool,
};

export { OAuthDs };
