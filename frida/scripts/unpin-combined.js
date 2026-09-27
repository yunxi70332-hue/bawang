/**************************************************************************************************
 *
 * This file defines various config parameters, used later within the other scripts.
 *
 * In all cases, you'll want to set CERT_PEM and likely PROXY_HOST and PROXY_PORT.
 *
 * Source available at https://github.com/httptoolkit/frida-interception-and-unpinning/
 * SPDX-License-Identifier: AGPL-3.0-or-later
 * SPDX-FileCopyrightText: Tim Perry <tim@httptoolkit.com>
 *
 *************************************************************************************************/

// Put your CA certificate data here in PEM format:
const CERT_PEM = `-----BEGIN CERTIFICATE-----
MIIFETCCA/mgAwIBAgIUOkl2iSzmLNvVqAPLtLk5WxAWItAwDQYJKoZIhvcNAQEL
BQAwgZUxCzAJBgNVBAYTAkNOMREwDwYDVQQIDAhTaGFuZ2hhaTERMA8GA1UEBwwI
U2hhbmdoYWkxFTATBgNVBAoMDFJlcWFibGUsIExMQzFJMBsGA1UECwwUaHR0cHM6
Ly9yZXFhYmxlLmNvbS8wKgYDVQQDDCNSZXFhYmxlIENBIChBdWcgMjgsIDIwMjYs
IDgxRUI4QjVEKTAeFw0yNjA4MjgwMjQwNTlaFw0zMzA1MzEwNTU1NDZaMIGVMQsw
CQYDVQQGEwJDTjERMA8GA1UECAwIU2hhbmdoYWkxETAPBgNVBAcMCFNoYW5naGFp
MRUwEwYDVQQKDAxSZXFhYmxlLCBMTEMxSTAbBgNVBAsMFGh0dHBzOi8vcmVxYWJs
ZS5jb20vMCoGA1UEAwwjUmVxYWJsZSBDQSAoQXVnIDI4LCAyMDI2LCA4MUVCOEI1
RCkwggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQCcPr6iOdndLfWX27Iv
50eqI3u9i7JF+9qJ6ZVy0oNewbBuDxRbomDO9qPA6yyAIMVQv2FMGGdItSTL2Rdp
DUH52xRIlrJdy6unYL6jpniTTbt6Mpt7fcHUIGRmR6vAyS4BJ+UsUPfffa6xdciz
LA9YQGycCjyJX1xPMWf0cw4m476rgAGXZ2bieN2n//MH5XVjhiUJS6pcJcY/zbvr
hgyEh8QF54kmQaa5CSbctQCKZJBtOJN8xNuQkRlUJ9YVBZ/DPrE2sT8aZ78iZoxI
UCPw2X0CPUzbvfZbqO5i0eCy47Wn05G1qCtJBsV8xRWc2yqSvJ8o/DLlNWVFroXL
q1UNAgMBAAGjggFVMIIBUTAdBgNVHQ4EFgQU8pGSM/80EVRYMbfHRefpv2lbZJYw
HwYDVR0jBBgwFoAU8pGSM/80EVRYMbfHRefpv2lbZJYwDwYDVR0TAQH/BAUwAwEB
/zAOBgNVHQ8BAf8EBAMCAgQwge0GCWCGSAGG+EIBDQSB3xaB3FRoaXMgUm9vdCBj
ZXJ0aWZpY2F0ZSB3YXMgZ2VuZXJhdGVkIGJ5IFJlcWFibGUgUHJveHkgZm9yIFNT
TCBQcm94eWluZy4gSWYgdGhpcyBjZXJ0aWZpY2F0ZSBpcyBwYXJ0IG9mIGEgY2Vy
dGlmaWNhdGUgY2hhaW4sIHRoaXMgbWVhbnMgdGhhdCB5b3UncmUgYnJvd3Npbmcg
dGhyb3VnaCBSZXFhYmxlIFByb3h5IHdpdGggU1NMIFByb3h5aW5nIGVuYWJsZWQg
Zm9yIHRoaXMgd2Vic2l0ZS4wDQYJKoZIhvcNAQELBQADggEBAEUP+L7RD/EcjQmp
VWhjSn9G/cpKGPKYu0QjW8zaA4hOjaRN9RHVMzicQ4WSS0LLiaizk5D9+BXgOBrH
quVJbeew5OZLKlCU6gK8JjZGdrRmB15JKg6hhCBBzHcVpDvAFgJoAE5whObnLKS9
dMEgXdLrJNeW7MwwdrK/OWmlRGvwn12aE2GvB9+3K//LNFqZB261xv1FC4GR9U32
55lmCsWXkiwqsv/oklJSiOHh1KF1Xm+FZTGQeLL7tLZN8lxekgiwXXalMAudwNfN
6c1KFqyGnNdEtpnsO9H1e+2DSukenfgs02AWjLBYqs18Nyg1itsOqwTNo4SM4CA1
dxaQmKk=
-----END CERTIFICATE-----`;

// Put your intercepting proxy's address here:
const PROXY_HOST = '127.0.0.1';
const PROXY_PORT = 9000;

// If you like, set to to true to enable extra logging:
const DEBUG_MODE = true;

// If you find issues with non-HTTP traffic being captured (due to the
// native connect hook script) you can add ports here to exempt traffic
// on that port from being redirected. Note that this will only affect
// traffic captured by the raw connection hook - for apps using the
// system HTTP proxy settings, traffic on these ports will still be
// sent via the proxy and intercepted despite this setting.
const IGNORED_NON_HTTP_PORTS = [];

// As HTTP/3 is often not well supported by MitM proxies, by default it
// is blocked entirely, so all outgoing UDP connections to port 443
// will fail. If this is set to false, they will instead be left unintercepted.
const BLOCK_HTTP3 = true;

// Set this to true if your proxy supports SOCKS5 connections.
// This makes it possible for native-connect-hook to redirect
// non-HTTP traffic through your proxy (to view it raw, and
// avoid breaking non-HTTP traffic en route).
const PROXY_SUPPORTS_SOCKS5 = false;


// ----------------------------------------------------------------------------
// You don't need to modify any of the below, it just checks and applies some
// of the configuration that you've entered above.
// ----------------------------------------------------------------------------


if (DEBUG_MODE) {
    // Add logging just for clean output & to separate reloads:
    console.log('\n*** Starting scripts ***');
    if (globalThis.Java?.available) {
        Java.perform(() => {
            setTimeout(() => console.log('*** Scripts completed ***\n'), 5);
            // (We assume that nothing else will take more than 5ms, but app startup
            // probably will, so this should separate script & runtime logs)
        });
    } else {
        setTimeout(() => console.log('*** Scripts completed ***\n'), 5);
        // (We assume that nothing else will take more than 5ms, but app startup
        // probably will, so this should separate script & runtime logs)
    }
} else {
    console.log(''); // Add just a single newline, for minimal clarity
}

// Check the certificate (without literally including the instruction phrasing
// here, as that can be confusing for some users):
if (CERT_PEM.match(/\[!!.* CA certificate data .* !!\]/)) {
    throw new Error('No certificate was provided' +
        '\n\n' +
        'You need to set CERT_PEM in the Frida config script ' +
        'to the contents of your CA certificate.'
    );
}



// ----------------------------------------------------------------------------
// Don't modify any of the below unless you know what you're doing!
// This section defines various utilities & calculates some constants which may
// be used by later scripts elsewhere in this project.
// ----------------------------------------------------------------------------



// As web atob & Node.js Buffer aren't available, we need to reimplement base64 decoding
// in pure JS. This is a quick rough implementation without much error handling etc!

// Base64 character set (plus padding character =) and lookup:
const BASE64_CHARS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=';
const BASE64_LOOKUP = new Uint8Array(123);
for (let i = 0; i < BASE64_CHARS.length; i++) {
    BASE64_LOOKUP[BASE64_CHARS.charCodeAt(i)] = i;
}


/**
 * Take a base64 string, and return the raw bytes
 * @param {string} input
 * @returns Uint8Array
 */
function decodeBase64(input) {
    // Calculate the length of the output buffer based on padding:
    let outputLength = Math.floor((input.length * 3) / 4);
    if (input[input.length - 1] === '=') outputLength--;
    if (input[input.length - 2] === '=') outputLength--;

    const output = new Uint8Array(outputLength);
    let outputPos = 0;

    // Process each 4-character block:
    for (let i = 0; i < input.length; i += 4) {
        const a = BASE64_LOOKUP[input.charCodeAt(i)];
        const b = BASE64_LOOKUP[input.charCodeAt(i + 1)];
        const c = BASE64_LOOKUP[input.charCodeAt(i + 2)];
        const d = BASE64_LOOKUP[input.charCodeAt(i + 3)];

        // Assemble into 3 bytes:
        const chunk = (a << 18) | (b << 12) | (c << 6) | d;

        // Add each byte to the output buffer, unless it's padding:
        output[outputPos++] = (chunk >> 16) & 0xff;
        if (input.charCodeAt(i + 2) !== 61) output[outputPos++] = (chunk >> 8) & 0xff;
        if (input.charCodeAt(i + 3) !== 61) output[outputPos++] = chunk & 0xff;
    }

    return output;
}

/**
 * Take a single-certificate PEM string, and return the raw DER bytes
 * @param {string} input
 * @returns Uint8Array
 */
function pemToDer(input) {
    const pemLines = input.split('\n');
    if (
        pemLines[0] !== '-----BEGIN CERTIFICATE-----' ||
        pemLines[pemLines.length- 1] !== '-----END CERTIFICATE-----'
    ) {
        throw new Error(
            'Your certificate should be in PEM format, starting & ending ' +
            'with a BEGIN CERTIFICATE & END CERTIFICATE header/footer'
        );
    }

    const base64Data = pemLines.slice(1, -1).map(l => l.trim()).join('');
    if ([...base64Data].some(c => !BASE64_CHARS.includes(c))) {
        throw new Error(
            'Your certificate should be in PEM format, containing only ' +
            'base64 data between a BEGIN & END CERTIFICATE header/footer'
        );
    }

    return decodeBase64(base64Data);
}

const CERT_DER = pemToDer(CERT_PEM);

// Calls the callback with the Frida Module, either immediately if it's already loaded, or as
// soon as it is:
function waitForModule(moduleName, callback) {
    if (Array.isArray(moduleName)) {
        moduleName.forEach(module => waitForModule(module, callback));
        return;
    }

    const module = findLoadedModule(moduleName);

    if (module) {
        callback(module);
        return;
    }

    if (!MODULE_LOAD_CALLBACKS[moduleName]) MODULE_LOAD_CALLBACKS[moduleName] = [];
    MODULE_LOAD_CALLBACKS[moduleName].push(callback);
}

function findLoadedModule(moduleName) {
    try {
        const module = Process.getModuleByName(moduleName);
        module.ensureInitialized();
        return module;
    } catch (e) {}

    try {
        return Module.load(moduleName);
    } catch (e) {}

    return null;
}

const getModuleName = (nameOrPath) => {
    const endOfPath = nameOrPath.lastIndexOf('/');
    return nameOrPath.slice(endOfPath + 1);
};

const MODULE_LOAD_CALLBACKS = {};

// Frida notifies us as libraries are loaded. We use this rather than hooking dlopen ourselves,
// because on Android 8 & older dlopen works out the calling library's linker namespace from its
// return address - which an inline hook necessarily changes, breaking library loading (and so
// the app) entirely. This also spots libraries loaded straight from an APK, which can't be
// looked up by name afterwards.
Process.attachModuleObserver({
    onAdded(module) {
        const moduleName = getModuleName(module.name || module.path || '');
        const callbacks = MODULE_LOAD_CALLBACKS[moduleName];
        if (!callbacks) return;

        delete MODULE_LOAD_CALLBACKS[moduleName];
        callbacks.forEach((callback) => callback(module));
    }
});/**************************************************************************************************
 *
 * This script defines a large set of targeted certificate unpinning hooks: matching specific
 * methods in certain classes, and transforming their behaviour to ensure that restrictions to
 * TLS trust are disabled.
 *
 * This does not disable TLS protections completely - each hook is designed to disable only
 * *additional* restrictions, and to explicitly trust the certificate provided as CERT_PEM in the
 * config.js configuration file, preserving normal TLS protections wherever possible, even while
 * allowing for controlled MitM of local traffic.
 *
 * The file consists of a few general-purpose methods, then a data structure declaratively
 * defining the classes & methods to match, and how to transform them, and then logic at the end
 * which uses this data structure, applying the transformation for each found match to the
 * target process.
 *
 * For more details on what was matched, and log output when each hooked method is actually used,
 * enable DEBUG_MODE in config.js, and watch the Frida output after running this script.
 *
 * Source available at https://github.com/httptoolkit/frida-interception-and-unpinning/
 * SPDX-License-Identifier: AGPL-3.0-or-later
 * SPDX-FileCopyrightText: Tim Perry <tim@httptoolkit.com>
 *
 *************************************************************************************************/

function buildX509CertificateFromBytes(certBytes) {
    const ByteArrayInputStream = Java.use('java.io.ByteArrayInputStream');
    const CertFactory = Java.use('java.security.cert.CertificateFactory');
    const certFactory = CertFactory.getInstance("X.509");
    return certFactory.generateCertificate(ByteArrayInputStream.$new(certBytes));
}

function getCustomTrustManagerFactory() {
    // This is the one X509Certificate that we want to trust. No need to trust others (we should capture
    // _all_ TLS traffic) and risky to trust _everything_ (risks interception between device & proxy, or
    // worse: some traffic being unintercepted & sent as HTTPS with TLS effectively disabled over the
    // real web - potentially exposing auth keys, private data and all sorts).
    const certBytes = Java.use("java.lang.String").$new(CERT_PEM).getBytes();
    const trustedCACert = buildX509CertificateFromBytes(certBytes);

    // Build a custom TrustManagerFactory with a KeyStore that trusts only this certificate:

    const KeyStore = Java.use("java.security.KeyStore");
    const keyStore = KeyStore.getInstance(KeyStore.getDefaultType());
    keyStore.load(null);
    keyStore.setCertificateEntry("ca", trustedCACert);

    const TrustManagerFactory = Java.use("javax.net.ssl.TrustManagerFactory");
    const customTrustManagerFactory = TrustManagerFactory.getInstance(
        TrustManagerFactory.getDefaultAlgorithm()
    );
    customTrustManagerFactory.init(keyStore);

    return customTrustManagerFactory;
}

function getCustomX509TrustManager() {
    const customTrustManagerFactory = getCustomTrustManagerFactory();
    const trustManagers = customTrustManagerFactory.getTrustManagers();

    const X509TrustManager = Java.use('javax.net.ssl.X509TrustManager');

    const x509TrustManager = trustManagers.find((trustManager) => {
        return trustManager.class.isAssignableFrom(X509TrustManager.class);
    });

    // We have to cast it explicitly before Frida will allow us to use the X509 methods:
    return Java.cast(x509TrustManager, X509TrustManager);
}

// Some standard hook replacements for various cases:
const NO_OP = () => {};
const RETURN_TRUE = () => true;
const RETURN_FALSE = () => false;
const CHECK_OUR_TRUST_MANAGER_ONLY = () => {
    const trustManager = getCustomX509TrustManager();
    return (certs, authType) => {
        trustManager.checkServerTrusted(certs, authType);
    };
};

const PINNING_FIXES = {
    // --- Native HttpsURLConnection

    'javax.net.ssl.HttpsURLConnection': [
        {
            methodName: 'setDefaultHostnameVerifier',
            replacement: () => NO_OP
        },
        {
            methodName: 'setSSLSocketFactory',
            replacement: () => NO_OP
        },
        {
            methodName: 'setHostnameVerifier',
            replacement: () => NO_OP
        },
    ],

    // --- Native SSLContext

    'javax.net.ssl.SSLContext': [
        {
            methodName: 'init',
            overload: ['[Ljavax.net.ssl.KeyManager;', '[Ljavax.net.ssl.TrustManager;', 'java.security.SecureRandom'],
            replacement: (targetMethod) => {
                const customTrustManagerFactory = getCustomTrustManagerFactory();

                // When constructor is called, replace the trust managers argument:
                return function (keyManager, _providedTrustManagers, secureRandom) {
                    return targetMethod.call(this,
                        keyManager,
                        customTrustManagerFactory.getTrustManagers(), // Override their trust managers
                        secureRandom
                    );
                }
            }
        }
    ],

    // --- Native Conscrypt CertPinManager

    'com.android.org.conscrypt.CertPinManager': [
        {
            methodName: 'isChainValid',
            replacement: () => RETURN_TRUE
        },
        {
            methodName: 'checkChainPinning',
            replacement: () => NO_OP
        }
    ],

    // --- Native pinning configuration loading (used for configuration by many libraries)

    'android.security.net.config.NetworkSecurityConfig': [
        {
            methodName: '$init',
            overload: '*',
            replacement: (targetMethod) => {
                const PinSet = Java.use('android.security.net.config.PinSet');
                const EMPTY_PINSET = PinSet.EMPTY_PINSET.value;

                // The pins position in the constructor moves between Android versions (Android
                // 15 inserted a certificate transparency argument) so match args by type:
                const pinsIndex = targetMethod.argumentTypes.findIndex(
                    (argType) => argType.className === 'android.security.net.config.PinSet'
                );

                if (pinsIndex === -1) {
                    console.warn('[!] No PinSet argument in NetworkSecurityConfig constructor - ' +
                        'config-defined certificate pinning will not be disabled');
                }

                return function () {
                    // Always ignore the 'pins' PinSet argument entirely:
                    if (pinsIndex !== -1) arguments[pinsIndex] = EMPTY_PINSET;
                    targetMethod.call(this, ...arguments);
                }
            }
        }
    ],

    // --- Native HostnameVerification override (n.b. Android contains its own vendored OkHttp v2!)

    'com.android.okhttp.internal.tls.OkHostnameVerifier': [
        {
            methodName: 'verify',
            overload: [
                'java.lang.String',
                'javax.net.ssl.SSLSession'
            ],
            replacement: (targetMethod) => {
                // Our trust manager - this trusts *only* our extra CA
                const trustManager = getCustomX509TrustManager();

                return function (hostname, sslSession) {
                    try {
                        const certs = sslSession.getPeerCertificates();

                        // https://stackoverflow.com/a/70469741/68051
                        const authType = "RSA";

                        // This throws if the certificate isn't trusted (i.e. if it's
                        // not signed by our extra CA specifically):
                        trustManager.checkServerTrusted(certs, authType);

                        // If the cert is from our CA, great! Skip hostname checks entirely.
                        return true;
                    } catch (e) {} // Ignore errors and fallback to default behaviour

                    // We fallback to ensure that connections with other CAs (e.g. direct
                    // connections allowed past the proxy) validate as normal.
                    return targetMethod.call(this, ...arguments);
                }
            }
        }
    ],

    'com.android.okhttp.Address': [
        {
            methodName: '$init',
            overload: [
                'java.lang.String',
                'int',
                'com.android.okhttp.Dns',
                'javax.net.SocketFactory',
                'javax.net.ssl.SSLSocketFactory',
                'javax.net.ssl.HostnameVerifier',
                'com.android.okhttp.CertificatePinner',
                'com.android.okhttp.Authenticator',
                'java.net.Proxy',
                'java.util.List',
                'java.util.List',
                'java.net.ProxySelector'
            ],
            replacement: (targetMethod) => {
                const defaultHostnameVerifier = Java.use("com.android.okhttp.internal.tls.OkHostnameVerifier")
                    .INSTANCE.value;
                const defaultCertPinner = Java.use("com.android.okhttp.CertificatePinner")
                    .DEFAULT.value;

                return function () {
                    // Override arguments, to swap any custom check params (widely used
                    // to add stricter rules to TLS verification) with the defaults instead:
                    arguments[5] = defaultHostnameVerifier;
                    arguments[6] = defaultCertPinner;

                    targetMethod.call(this, ...arguments);
                }
            }
        },
        // Almost identical patch, but for Nougat and older. In these versions, the DNS argument
        // isn't passed here, so the arguments to patch changes slightly:
        {
            methodName: '$init',
            overload: [
                'java.lang.String',
                'int',
                // No DNS param
                'javax.net.SocketFactory',
                'javax.net.ssl.SSLSocketFactory',
                'javax.net.ssl.HostnameVerifier',
                'com.android.okhttp.CertificatePinner',
                'com.android.okhttp.Authenticator',
                'java.net.Proxy',
                'java.util.List',
                'java.util.List',
                'java.net.ProxySelector'
            ],
            replacement: (targetMethod) => {
                const defaultHostnameVerifier = Java.use("com.android.okhttp.internal.tls.OkHostnameVerifier")
                    .INSTANCE.value;
                const defaultCertPinner = Java.use("com.android.okhttp.CertificatePinner")
                    .DEFAULT.value;

                return function () {
                    // Override arguments, to swap any custom check params (widely used
                    // to add stricter rules to TLS verification) with the defaults instead:
                    arguments[4] = defaultHostnameVerifier;
                    arguments[5] = defaultCertPinner;

                    targetMethod.call(this, ...arguments);
                }
            }
        }
    ],

    // --- OkHttp v3

    'okhttp3.CertificatePinner': [
        {
            methodName: 'check',
            overload: ['java.lang.String', 'java.util.List'],
            replacement: () => NO_OP
        },
        {
            methodName: 'check',
            overload: ['java.lang.String', 'java.security.cert.Certificate'],
            replacement: () => NO_OP
        },
        {
            methodName: 'check',
            overload: ['java.lang.String', '[Ljava.security.cert.Certificate;'],
            replacement: () => NO_OP
        },
        {
            methodName: 'check$okhttp',
            replacement: () => NO_OP
        },
    ],

    // --- SquareUp OkHttp (< v3)

    'com.squareup.okhttp.CertificatePinner': [
        {
            methodName: 'check',
            overload: ['java.lang.String', 'java.security.cert.Certificate'],
            replacement: () => NO_OP
        },
        {
            methodName: 'check',
            overload: ['java.lang.String', 'java.util.List'],
            replacement: () => NO_OP
        }
    ],

    // --- Trustkit (https://github.com/datatheorem/TrustKit-Android/)

    'com.datatheorem.android.trustkit.pinning.PinningTrustManager': [
        {
            methodName: 'checkServerTrusted',
            replacement: CHECK_OUR_TRUST_MANAGER_ONLY
        }
    ],

    // --- Appcelerator (https://github.com/tidev/appcelerator.https)

    'appcelerator.https.PinningTrustManager': [
        {
            methodName: 'checkServerTrusted',
            replacement: CHECK_OUR_TRUST_MANAGER_ONLY
        }
    ],

    // --- PhoneGap sslCertificateChecker (https://github.com/EddyVerbruggen/SSLCertificateChecker-PhoneGap-Plugin)

    'nl.xservices.plugins.sslCertificateChecker': [
        {
            methodName: 'execute',
            overload: ['java.lang.String', 'org.json.JSONArray', 'org.apache.cordova.CallbackContext'],
            replacement: () => (_action, _args, context) => {
                context.success("CONNECTION_SECURE");
                return true;
            }
            // This trusts _all_ certs, but that's fine - this is used for checks of independent test
            // connections, rather than being a primary mechanism to secure the app's TLS connections.
        }
    ],

    // --- IBM WorkLight

    'com.worklight.wlclient.api.WLClient': [
        {
            methodName: 'pinTrustedCertificatePublicKey',
            getMethod: (WLClientCls) => WLClientCls.getInstance().pinTrustedCertificatePublicKey,
            overload: '*'
        }
    ],

    'com.worklight.wlclient.certificatepinning.HostNameVerifierWithCertificatePinning': [
        {
            methodName: 'verify',
            overload: '*',
            replacement: () => NO_OP
        }
        // This covers at least 4 commonly used WorkLight patches. Oddly, most sets of hooks seem
        // to return true for 1/4 cases, which must be wrong (overloads must all have the same
        // return type) but also it's very hard to find any modern (since 2017) references to this
        // class anywhere including WorkLight docs, so it may no longer be relevant anyway.
    ],

    'com.worklight.androidgap.plugin.WLCertificatePinningPlugin': [
        {
            methodName: 'execute',
            overload: '*',
            replacement: () => RETURN_TRUE
        }
    ],

    // --- CWAC-Netsecurity (unofficial back-port pinner for Android<4.2) CertPinManager

    'com.commonsware.cwac.netsecurity.conscrypt.CertPinManager': [
        {
            methodName: 'isChainValid',
            overload: '*',
            replacement: () => RETURN_TRUE
        }
    ],

    // --- Netty

    'io.netty.handler.ssl.util.FingerprintTrustManagerFactory': [
        {
            methodName: 'checkTrusted',
            replacement: () => NO_OP
        }
    ],

    // --- Cordova / PhoneGap Advanced HTTP Plugin (https://github.com/silkimen/cordova-plugin-advanced-http)

    // Modern version:
    'com.silkimen.cordovahttp.CordovaServerTrust': [
        {
            methodName: '$init',
            replacement: (targetMethod) => function () {
                // Ignore any attempts to set trust to 'pinned'. Default settings will trust
                // our cert because of the separate system-certificate injection step.
                if (arguments[0] === 'pinned') {
                    arguments[0] = 'default';
                }

                return targetMethod.call(this, ...arguments);
            }
        }
    ],

    // --- Appmattus Cert Transparency (https://github.com/appmattus/certificatetransparency/)

    'com.appmattus.certificatetransparency.internal.verifier.CertificateTransparencyBase': [
        {
            methodName: 'enabledForCertificateTransparency',
            replacement: () => RETURN_FALSE
        }
    ],

    'com.appmattus.certificatetransparency.internal.verifier.CertificateTransparencyTrustManagerBasic': [
        {
            methodName: 'checkServerTrusted',
            overload: ['[Ljava.security.cert.X509Certificate;', 'java.lang.String'],
            replacement: CHECK_OUR_TRUST_MANAGER_ONLY
        },
        {
            methodName: 'checkServerTrusted',
            overload: ['[Ljava.security.cert.X509Certificate;', 'java.lang.String', 'java.lang.String'],
            replacement: () => {
                const trustManager = getCustomX509TrustManager();
                return (certs, authType, _hostname) => {
                    // We ignore the hostname - if the certs are good (i.e they're ours), then the
                    // whole chain is good to go.
                    trustManager.checkServerTrusted(certs, authType);
                    return Java.use('java.util.Arrays').asList(certs);
                };
            }
        }
    ],

    // --- Cronet (Chromium's network stack - does its own native TLS, bypassing the hooks above)

    'org.chromium.net.CronetEngine$Builder': [
        {
            methodName: 'enablePublicKeyPinningBypassForLocalTrustAnchors',
            replacement: (targetMethod) => function (_enabled) {
                // Ignore the app's choice and always allow local trust anchors to bypass
                // pinning - the method returns the builder, so preserve that for chaining:
                return targetMethod.call(this, true);
            }
        }
    ]

};

const getJavaClassIfExists = (clsName) => {
    try {
        const TargetClass = Java.use(clsName);

        // Hooks applied to a class that hasn't been initialized yet can be silently dropped when
        // the runtime does eventually initialize it - patches then appear to apply, but never
        // take effect (seen on Android 11 with app-bundled libraries, which are only initialized
        // on first use, i.e. long after we get here). Initializing it now avoids that:
        try {
            Java.use('java.lang.Class').forName(
                clsName,
                true, // Initialize
                TargetClass.class.getClassLoader()
            );
        } catch (e) {
            // A class whose initializer fails is still worth patching, so this isn't fatal:
            if (DEBUG_MODE) console.log(`[ ] Could not initialize ${clsName}: ${e.message}`);
        }

        return TargetClass;
    } catch {
        return undefined;
    }
}

Java.perform(function () {
    if (DEBUG_MODE) console.log('\n    === Disabling all recognized unpinning libraries ===');

    const classesToPatch = Object.keys(PINNING_FIXES);

    classesToPatch.forEach((targetClassName) => {
        const TargetClass = getJavaClassIfExists(targetClassName);
        if (!TargetClass) {
            // We skip patches for any classes that don't seem to be present. This is common
            // as not all libraries we handle are necessarily used.
            if (DEBUG_MODE) console.log(`[ ] ${targetClassName} *`);
            return;
        }

        const patches = PINNING_FIXES[targetClassName];

        let patchApplied = false;

        patches.forEach(({ methodName, getMethod, overload, replacement }) => {
            const namedTargetMethod = getMethod
                ? getMethod(TargetClass)
                : TargetClass[methodName];

            const methodDescription = `${methodName}${
                overload === '*'
                    ? '(*)'
                : overload
                    ? '(' + overload.map((argType) => {
                        // Simplify arg names to just the class name for simpler logs:
                        const argClassName = argType.split('.').slice(-1)[0];
                        if (argType.startsWith('[L')) return `${argClassName}[]`;
                        else return argClassName;
                    }).join(', ') + ')'
                // No overload:
                    : ''
            }`

            let targetMethodImplementations = [];
            try {
                if (namedTargetMethod) {
                    if (!overload) {
                            // No overload specified
                        targetMethodImplementations = [namedTargetMethod];
                    } else if (overload === '*') {
                        // Targetting _all_ overloads
                        targetMethodImplementations = namedTargetMethod.overloads;
                    } else {
                        // Or targetting a specific overload:
                        targetMethodImplementations = [namedTargetMethod.overload(...overload)];
                    }
                }
            } catch (e) {
                // Overload not present
            }


            // We skip patches for any methods that don't seem to be present. This is rarer, but does
            // happen due to methods that only appear in certain library versions or whose signatures
            // have changed over time.
            if (targetMethodImplementations.length === 0) {
                if (DEBUG_MODE) console.log(`[ ] ${targetClassName} ${methodDescription}`);
                return;
            }

            targetMethodImplementations.forEach((targetMethod, i) => {
                const patchName = `${targetClassName} ${methodDescription}${
                    targetMethodImplementations.length > 1 ? ` (${i})` : ''
                }`;

                try {
                    const newImplementation = replacement(targetMethod);
                    if (DEBUG_MODE) {
                        // Log each hooked method as it's called:
                        targetMethod.implementation = function () {
                            console.log(` => ${patchName}`);
                            return newImplementation.apply(this, arguments);
                        }
                    } else {
                        targetMethod.implementation = newImplementation;
                    }

                    if (DEBUG_MODE) console.log(`[+] ${patchName}`);
                    patchApplied = true;
                } catch (e) {
                    // In theory, errors like this should never happen - it means the patch is broken
                    // (e.g. some dynamic patch building fails completely)
                    console.error(`[!] ERROR: ${patchName} failed: ${e}`);
                }
            })
        });

        if (!patchApplied) {
            console.warn(`[!] Matched class ${targetClassName} but could not patch any methods`);
        }
    });

    console.log('== Certificate unpinning completed ==');
});
/**************************************************************************************************
 *
 * This script hooks Flutter internal certificate handling, to trust our certificate (and ignore
 * any custom certificate validation - e.g. pinning libraries) for all TLS connections.
 *
 * Unfortunately Flutter is shipped as native code with no exported symbols, so we have to do this
 * by matching individual function signatures by known patterns of assembly instructions. In
 * some cases, this goes further and uses larger functions as anchors - allowing us to find the
 * very short functions correctly, where the patterns would otherwise have false positives.
 *
 * Flutter ships a separate engine build per mode: apps run with `flutter run` bundle the JIT
 * 'debug' engine, while `flutter build --release` bundles a distinct AOT 'release' engine, built
 * with LTO (and, on arm64, the LLVM machine outliner) so that the same functions compile to
 * visibly different code. We therefore carry a separate set of patterns for each, and detect
 * which engine is loaded at runtime.
 *
 * In release builds LTO also inlines X509_STORE_CTX_get_current_cert (a one-line accessor) into
 * its callers, so there is no function left to call. Instead we locate the instruction that reads
 * the field inside CertificateCallback and recover the struct offset from it, then read the
 * certificate out of the store directly.
 *
 * The patterns here have been generated from every non-patch release of Flutter from v2.0.0
 * to v3.44.0 (the latest at the time of writing). They may need updates for new versions
 * in future.
 *
 * Currently this is limited to just Android, but in theory this can be expanded to iOS and
 * desktop platforms in future.
 *
 * Source available at https://github.com/httptoolkit/frida-interception-and-unpinning/
 * SPDX-License-Identifier: AGPL-3.0-or-later
 * SPDX-FileCopyrightText: Tim Perry <tim@httptoolkit.com>
 *
 *************************************************************************************************/

(() => {
    const PATTERNS = {
        "android/x64": {
            "dart::bin::SSLCertContext::CertificateCallback": {
                "signatures": [
                    "41 57 41 56 41 54 53 48 83 ec 18 b8 01 00 00 00 83 ff 01 0f 84 ?? ?? ?? ?? 48 89 f3",
                    "41 57 41 56 53 48 83 ec 10 b8 01 00 00 00 83 ff 01 0f 84 ?? ?? ?? ?? 48 89 f3"
                ]
            },
            "X509_STORE_CTX_get_current_cert": {
                "signatures": [
                    "48 8b 87 b8 00 00 00 c3",
                    "48 8b 47 60 c3",
                    "48 8b 87 a8 00 00 00 c3",
                    "48 8b 47 50 c3"
                ],
                "anchor": "dart::bin::SSLCertContext::CertificateCallback"
            },
            "bssl::x509_to_buffer": {
                "signatures": [
                    "41 57 41 56 53 48 83 ec 10 48 89 f0 49 89 fe 48 89 e6 48 83 26 00 48 89 c7 e8",
                    "41 56 53 50 48 89 f0 48 89 fb 48 89 e6 48 83 26 00 48 89 c7 e8 ?? ?? ?? ?? 85 c0 7e 1b",
                    "53 48 83 ec 10 48 89 f0 48 89 fb 48 8d 74 24 08 48 83 26 00 48 89 c7 e8 ?? ?? ?? ?? 85 c0",
                    "41 56 53 48 83 ec 18 48 89 f0 4? 89 f? 48 8d 74 24 08 48 83 26 00 48 89 c7 e8"
                ]
            },
            "i2d_X509": {
                "signatures": [
                    "48 8d 15 ?? ?? ?? ?? e9",
                    "55 41 56 53 48 83 ec 70 48 85 ff 0f 84 ?? ?? ?? ?? 48 89 f3 49 89 fe 48 8d 7c 24 40 6a 40",
                    "55 41 57 41 56 53 48 83 ec 68 48 85 ff 0f 84 ?? ?? ?? ?? 48 89 f3 49 89 fe 4c 8d 7c 24 08",
                    "55 41 57 41 56 53 48 83 ec 68 48 85 ff 0f 84 ?? ?? ?? ?? 49 89 f6 49 89 ff 48 8d 5c 24 38"
                ],
                "anchor": "bssl::x509_to_buffer"
            }
        },
        "android/x86": {
            "dart::bin::SSLCertContext::CertificateCallback": {
                "signatures": [
                    "55 89 e5 53 57 56 83 e4 f0 83 ec 30 e8 ?? ?? ?? ?? 5b 81 c3 ?? ?? ?? ?? bf 01 00 00 00 83 7d 08 01 0f 84"
                ]
            },
            "X509_STORE_CTX_get_current_cert": {
                "signatures": [
                    "55 89 e5 83 e4 fc 8b 45 08 8b 40 64 89 ec 5d c3",
                    "55 89 e5 83 e4 fc 8b 45 08 8b 40 34 89 ec 5d c3",
                    "55 89 e5 83 e4 fc 8b 45 08 8b 40 5c 89 ec 5d c3",
                    "55 89 e5 83 e4 fc 8b 45 08 8b 40 2c 89 ec 5d c3"
                ],
                "anchor": "dart::bin::SSLCertContext::CertificateCallback"
            },
            "bssl::x509_to_buffer": {
                "signatures": [
                    "55 89 e5 53 57 56 83 e4 f0 83 ec 20 89 ce e8 ?? ?? ?? ?? 5b 81 c3 ?? ?? ?? ?? 8d 44 24 14 83 20 00 89 44 24 04 89 14 24",
                    "55 89 e5 53 57 56 83 e4 f0 83 ec 10 89 ce e8 ?? ?? ?? ?? 5b 81 c3 ?? ?? ?? ?? 8d 44 24 08 83 20 00 83 ec 08 50 52",
                    "55 89 e5 53 56 83 e4 f0 83 ec 10 89 ce e8 ?? ?? ?? ?? 5b 81 c3 ?? ?? ?? ?? 8d 44 24 0c 83 20 00 83 ec 08 50 52"
                ]
            },
            "i2d_X509": {
                "signatures": [
                    "55 89 e5 53 83 e4 f0 83 ec 10 e8 ?? ?? ?? ?? 5b 81 c3 ?? ?? ?? ?? 83 ec 04 8d 83 ?? ?? ?? ?? 50 ff 75 0c ff 75 08",
                    "55 89 e5 53 57 56 83 e4 f0 83 ec 40 e8 ?? ?? ?? ?? 5b 81 c3 ?? ?? ?? ?? 8b 7d 08 85 ff 0f 84 ?? ?? ?? ?? 83 ec 08",
                    "55 89 e5 53 57 56 83 e4 f0 83 ec 40 e8 ?? ?? ?? ?? 5b 81 c3 ?? ?? ?? ?? 8b 75 08 83 ec 0c 85 f6 0f 84",
                    "55 89 e5 53 57 56 83 e4 f0 83 ec 40 e8 ?? ?? ?? ?? 5b 81 c3 ?? ?? ?? ?? 83 ec 0c 83 7d 08 00 0f 84"
                ],
                "anchor": "bssl::x509_to_buffer"
            }
        },
        "android/arm64": {
            "dart::bin::SSLCertContext::CertificateCallback": {
                "signatures": [
                    "ff c3 00 d1 fe 57 01 a9 f4 4f 02 a9 1f 04 00 71 ?0 ?? ?? 54 f3 03 01 aa ?? ?? ?? 94 e0 07 00 b4 e0 03 13 aa",
                    "ff c3 00 d1 fe 57 01 a9 f4 4f 02 a9 1f 04 00 71 ?0 ?? ?? 54 f3 03 01 aa ?? ?? ?? 94 c0 09 00 b4 e0 03 13 aa",
                    "ff c3 00 d1 fe 57 01 a9 f4 4f 02 a9 1f 04 00 71 ?0 ?? ?? 54 f3 03 01 aa ?? ?? ?? 94 00 0a 00 b4 e0 03 13 aa"
                ]
            },
            "X509_STORE_CTX_get_current_cert": {
                "signatures": [
                    "00 ?? ?? f9 c0 03 5f d6"
                ],
                "anchor": "dart::bin::SSLCertContext::CertificateCallback"
            },
            "bssl::x509_to_buffer": {
                "signatures": [
                    "fe 0f 1e f8 f4 4f 01 a9 e8 03 01 aa f3 03 00 aa e1 ?? ?? 91 e0 03 08 aa ff 07 00 f9 ?? ?? ?? 97 1f 04 00 71",
                    "f? ?? ?? ?? f? 4f 01 a9 e1 ?? ?? 91 f3 03 08 aa ff 07 00 f9 ?? ?? ?? 97 1f 0? 00 71 ?? ?? ?? 54 e8 ?? ?? f9",
                    "ff c3 00 d1 fe 7f 01 a9 f4 4f 02 a9 e1 ?? ?? 91 f3 03 08 aa ?? ?? ?? 97 1f 0? 00 71 ?? ?? ?? 54 e8 ?? ?? f9"
                ]
            },
            "i2d_X509": {
                "signatures": [
                    "ff 43 02 d1 fe 57 07 a9 f4 4f 08 a9 a0 06 00 b4 f4 03 00 aa f3 03 01 aa e0 ?? ?? 91 01 08 80 52 ?? ?? ?? 97",
                    "?2 ?? ?? ?? 42 ?? ?? 91 ?? ?? ?? 17",
                    "ff 03 02 d1 fe 33 00 f9 f4 4f 07 a9 40 04 00 b4 ?? ?? ?? 94 e0 03 00 91 01 20 80 52 ?? ?? ?? 97 20 03 00 34",
                    "ff 43 02 d1 fe 57 07 a9 f4 4f 08 a9 00 06 00 b4 f4 03 00 aa e0 ?? ?? 91 f3 03 01 aa ?? ?? ?? 97 e0 ?? ?? 91"
                ],
                "anchor": "bssl::x509_to_buffer"
            }
        },
        "android/arm": {
            "dart::bin::SSLCertContext::CertificateCallback": {
                "signatures": [
                    "70 b5 84 b0 01 28 ?? d1 01 20 04 b0 70 bd 0c 46 ?? f? ?? f? 00 28 ?? d0 20 46 ?? f? ?? f? 0? 46 ??"
                ]
            },
            "X509_STORE_CTX_get_current_cert": {
                "signatures": [
                    "40 6b 70 47",
                    "40 6e 70 47",
                    "c0 6d 70 47",
                    "c0 6a 70 47"
                ],
                "anchor": "dart::bin::SSLCertContext::CertificateCallback"
            },
            "bssl::x509_to_buffer": {
                "signatures": [
                    "?c b5 00 2? 0a 46 01 9? 01 a9 04 46 10 46 ?? f? ?? f? 0? 28 ?? d? 01 46 01 98 00 22 ?? f? ??"
                ]
            },
            "i2d_X509": {
                "signatures": [
                    "70 b5 8e b0 00 28 ?? d0 05 46 08 a8 0c 46 40 21 ?? f? ?? f? 00 28 ?? d0 ?? 4a 08 a8 02 a9 ?? f? ?? f? ?? b3",
                    "?? 4a 7a 44 ?? f? ??",
                    "70 b5 8e b0 ?? b3 08 ae 05 46 0c 46 30 46 ?? f? ?? f? 30 46 40 21 ?? f? ?? f? ?? b3 ?? 4a 08 a8 02 a9",
                    "70 b5 8e b0 ?? b3 02 ae 05 46 0c 46 30 46 ?? f? ?? f? 30 46 4f f4 80 71 ?? f? ?? f? ?? b3 ?? 4a 02 a8 08 a9"
                ],
                "anchor": "bssl::x509_to_buffer"
            }
        },
        "android-release/x64": {
            "dart::bin::SSLCertContext::CertificateCallback": {
                "signatures": [
                    "41 57 41 56 53 48 83 ec 20 b8 01 00 00 00 83 ff 01 0f 84 ?? ?? ?? ?? 48 89 f3",
                    "41 57 41 56 41 54 53 48 83 ec 18 b? 01 00 00 00 83 ff 01 0f 84 ?? ?? ?? ?? 4? 89",
                    "41 57 41 56 41 54 53 48 83 ec 18 41 b? 01 00 00 00 83 ff 01 0f 84 ?? ?? ?? ?? 4? 89"
                ]
            },
            "bssl::x509_to_buffer": {
                "signatures": [
                    "41 56 53 48 83 ec 18 48 89 f0 49 89 fe 48 8d 74 24 08 48 83 26 00 48 89 c7 e8",
                    "41 56 53 50 48 89 f0 48 89 fb 48 89 e6 48 83 26 00 48 89 c7 e8 ?? ?? ?? ?? 85 c0 7e 1b",
                    "41 57 41 56 53 48 83 ec 10 48 89 f0 48 89 fb 48 8d 74 24 08 48 83 26 00 48 89 c7",
                    "53 48 83 ec 10 48 89 f0 48 89 fb 48 8d 74 24 08 48 83 26 00 48 89 c7 e8 ?? ?? ?? ?? 85 c0"
                ]
            },
            "i2d_X509": {
                "signatures": [
                    "48 8d 15 ?? ?? ?? ?? e9",
                    "55 41 56 53 48 81 ec 80 00 00 00 48 85 ff 0f 84 ?? ?? ?? ?? 48 89 f3 49 89 fe",
                    "41 57 41 56 53 48 83 ec 60 48 85 ff 0f 84 ?? ?? ?? ?? 49 89 f6 49 89 ff 48 89 e7",
                    "55 41 57 41 56 41 54 53 48 81 ec a0 00 00 00 48 85 ff 0f 84 ?? ?? ?? ?? 48 89 f3 49 89 fe"
                ],
                "anchor": "bssl::x509_to_buffer"
            },
            "X509_STORE_CTX::current_cert": {
                "anchor": "dart::bin::SSLCertContext::CertificateCallback",
                "anchorMode": "within",
                "signatures": [
                    "4c 8b bb b8 00 00 00",
                    "4d 8b 7e 60",
                    "4d 8b be a8 00 00 00",
                    "4d 8b 7e 50",
                    "49 8b 9e b8 00 00 00",
                    "4c 8b 73 50",
                    "4d 8b be b8 00 00 00"
                ]
            }
        },
        "android-release/arm64": {
            "dart::bin::SSLCertContext::CertificateCallback": {
                "signatures": [
                    "ff 03 01 d1 fe 0b 00 f9 f6 57 02 a9 f4 4f 03 a9 1f 04 00 71 ?0 ?? ?? 54"
                ]
            },
            "bssl::x509_to_buffer": {
                "signatures": [
                    "ff c3 00 d1 fe 7f 01 a9 f4 4f 02 a9 e1 ?? ?? 91 f3 03 08 aa ?? ?? ?? 97 1f 04 00 71 ?b ?? ?? 54 e8 ?? ?? f9",
                    "fe 0f 1e f8 f4 4f 01 a9 ?? ?? ?? 94 ff 07 00 f9 ?? ?? ?? 97 1f 04 00 71 ?b ?? ?? 54 e8 ?? ?? f9 e1 03 00 2a",
                    "ff c3 00 d1 fe 57 01 a9 f4 4f 02 a9 ?? ?? ?? 94 ?? ?? ?? 94 ff 07 00 f9 ?? ?? ?? 97 1f 04 00 71 ?b ?? ?? 54",
                    "f? ?? ?? ?? f? 4f 01 a9 e8 03 01 aa f3 03 00 aa ?? ?? ?? 94 ff 07 00 f9 ?? ?? ?? 97 1f 0? 00 71 ?? ?? ?? 54",
                    "ff c3 00 d1 fe 7f 01 a9 f4 4f 02 a9 ?? ?? ?? 94 e1 ?? ?? 91 e0 03 08 aa ?? ?? ?? 97 1f 0? 00 71 ?? ?? ?? 54"
                ]
            },
            "i2d_X509": {
                "signatures": [
                    "?2 ?? ?? ?? 42 ?? ?? 91 ?? ?? ?? 17",
                    "ff 83 02 d1 fe 57 08 a9 f4 4f 09 a9 a0 06 00 b4 ?? ?? ?? 94 e0 ?? ?? 91 ?? ?? ?? 94 e0 05 00 34 02 02 80 52",
                    "ff 83 02 d1 fe 57 08 a9 f4 4f 09 a9 c0 06 00 b4 ?? ?? ?? 94 e0 ?? ?? 91 ?? ?? ?? 94 00 06 00 34 02 02 80 52",
                    "ff 03 02 d1 fe 33 00 f9 f4 4f 07 a9 c0 03 00 b4 ?? ?? ?? 94 e0 03 00 91 ?? ?? ?? 94 e0 02 00 34 02 02 80 52",
                    "ff 43 03 d1 fe 53 00 f9 f6 57 0b a9 f4 4f 0c a9 60 09 00 b4 00 e4 00 6f ?? ?? ?? 94 ?? ?? ?? 94 e0 09 00 34"
                ],
                "anchor": "bssl::x509_to_buffer"
            },
            "X509_STORE_CTX::current_cert": {
                "anchor": "dart::bin::SSLCertContext::CertificateCallback",
                "anchorMode": "within",
                "signatures": [
                    "74 ?? ?? f9"
                ]
            }
        },
        "android-release/arm": {
            "dart::bin::SSLCertContext::CertificateCallback": {
                "signatures": [
                    "f0 b5 83 b0 01 28 ?? d1 01 20 03 b0 f0 bd ?? 48 0d 46 78 44 00 68 00 28 18 bf 82 f1 64 e9 ?? 48 78 44",
                    "f0 b5 83 b0 01 28 ?? d1 01 20 03 b0 f0 bd ?? 48 0? 46 78 44 00 68 00 28 18 bf ?0 f1 ?? eb ?? 48 78 44",
                    "f0 b5 83 b0 01 28 ?? d1 01 20 03 b0 f0 bd ?? 48 0c 46 78 44 00 68 00 28 18 bf 7? f1 ?? e? ?? 48 78 44",
                    "f0 b5 83 b0 01 28 ?? d1 01 20 03 b0 f0 bd ?? 48 0d 46 78 44 00 68 00 28 18 bf ?? f1 ?0 e? ?? 48 78 44",
                    "?0 b5 8? b0 01 28 ?? d1 01 20 0? b0 ?0 bd ?? 48 0? 46 78 44 ?? f? ?? e? 00 68 00 28 1c bf d0 f8 ?? 0? 00 28",
                    "f0 b5 83 b0 01 28 ?? d1 01 20 03 b0 f0 bd ?? 48 0d 46 78 44 00 68 00 28 18 bf 3? f1 ?? e? ?? 48 78 44"
                ]
            },
            "bssl::x509_to_buffer": {
                "signatures": [
                    "7c b5 00 2? 0a 46 01 9? 01 a9 04 46 10 46 ?? f? ?? f? 0? 28 ?? d? 01 ?? 01 ?? 00 22 ?? ?? ?? f? ?? ??",
                    "?c b5 00 2? 0a 46 01 9? 01 a9 04 46 10 46 ?? f? ?? f? 0? 28 ?? d? 01 46 01 98 00 22 ?? f? ?? f? ?? ??"
                ]
            },
            "i2d_X509": {
                "signatures": [
                    "?? 4a 7a 44 ?? f? ??",
                    "70 b5 90 b0 00 28 ?? d0 05 46 08 a8 0c 46 40 21 ?? f? ?? f? 00 28 ?? d0 ?? 4a 08 a8 02 a9 ?? f? ?? f? 00 28",
                    "b0 b5 8c b0 ?? b3 0c 46 05 46 68 46 4f f4 80 71 ?? f? ?? f? ?? b3 ?? 4a 06 a9 68 46 ?? f? ?? f? ?? b1 06 a8",
                    "f0 b5 95 b0 00 28 ?? d0 05 46 c0 ef 50 00 08 a8 0c 46 00 22 01 46 0d 92 41 f9 cd 0a 0a 60 40 21 ?? f? ??"
                ],
                "anchor": "bssl::x509_to_buffer"
            },
            "X509_STORE_CTX::current_cert": {
                "anchor": "dart::bin::SSLCertContext::CertificateCallback",
                "anchorMode": "within",
                "signatures": [
                    "65 6e 40 68",
                    "6c 6b 40 68",
                    "ec 6d 40 68",
                    "ec 6a 40 68",
                    "6c 6e 40 68",
                    "e5 6a 40 68"
                ]
            }
        }
    }


    // Not a function, but the instruction inside CertificateCallback that reads the field:
    const CURRENT_CERT_FIELD = 'X509_STORE_CTX::current_cert';

    const MAX_ANCHOR_INSTRUCTIONS_TO_SCAN = 100;

    // How much of CertificateCallback we scan to find the inlined field load. Every build
    // we've seen compiles it to well under this.
    const MAX_FUNCTION_BYTES_TO_SCAN = 0x400;

    const CALL_MNEMONICS = ['call', 'bl', 'blx'];

    // On ARM all of this code is Thumb, and both NativeFunction and Instruction.parse need the
    // low bit set to treat an address as Thumb rather than A32.
    const isArm32 = Process.arch === 'arm';
    const asCode = (address) => isArm32 ? address.or(1) : address;

    function scanForSignature(base, size, patterns) {
        const results = [];
        for (const pattern of patterns) {
            const result = Memory.scanSync(base, size, pattern);
            results.push(...result);
        }
        return results;
    }

    /**
     * Finds a function that we're going to call or hook, so its address has to be exactly
     * right: we require one unambiguous match, and fail loudly otherwise.
     *
     * Where the function is anchored, the anchor's call target is the function entry by
     * definition, so we can confirm the address outright: we accept a signature only if it
     * matches at the call target itself, never part-way into it.
     */
    function scanForFunction(moduleRXRanges, platformPatterns, functionName, anchorFn) {
        const patternInfo = platformPatterns[functionName];
        const signatures = patternInfo.signatures;

        if (patternInfo.anchor) {
            const maxPatternByteLength = Math.max(...signatures.map(p => (p.length + 1) / 3));

            let addr = asCode(ptr(anchorFn));

            for (let i = 0; i < MAX_ANCHOR_INSTRUCTIONS_TO_SCAN; i++) {
                const instr = Instruction.parse(addr);
                addr = instr.next;
                if (CALL_MNEMONICS.includes(instr.mnemonic)) {
                    const callTargetAddr = ptr(instr.operands[0].value);
                    const results = scanForSignature(callTargetAddr, maxPatternByteLength, signatures);
                    if (results.some(result => result.address.equals(callTargetAddr))) {
                        return callTargetAddr;
                    }
                }
            }

            throw new Error(`Failed to find any match for ${functionName} anchored by ${anchorFn}`);
        } else {
            const results = moduleRXRanges.flatMap((range) => scanForSignature(range.base, range.size, signatures));

            if (results.length !== 1) {
                // Not necessarily a problem: we scan with each build's patterns in turn, so
                // failing to match here is how we recognise the other kind of build.
                if (DEBUG_MODE) console.log(`Matches for ${functionName}:`, results);
                throw new Error(`Found ${results.length} matches for ${functionName}`);
            }

            return results[0].address;
        }
    }

    /**
     * Finds a function that's only used as a starting point to scan forwards from, never
     * called. That means we don't need its exact entry point, which matters because
     * signatures overlap here: one generated from a build with a shorter prologue also
     * matches part-way into the same function in a build with a longer one.
     *
     * We only tolerate matches that fall inside the extent of the first match, which proves
     * they cover the same code rather than a second, unrelated site.
     */
    function scanForAnchor(moduleRXRanges, platformPatterns, functionName) {
        const signatures = platformPatterns[functionName].signatures;
        const results = moduleRXRanges
            .flatMap((range) => scanForSignature(range.base, range.size, signatures))
            .sort((a, b) => a.address.compare(b.address));

        if (results.length === 0) throw new Error(`Failed to find any match for ${functionName}`);

        const firstMatchEnd = results[0].address.add(results[0].size);
        const overlapping = results.every(result => result.address.compare(firstMatchEnd) < 0);

        if (!overlapping) {
            throw new Error(`Found ${results.length} separate matches for ${functionName}`);
        }

        return results[0].address;
    }

    /**
     * Recovers the offset of X509_STORE_CTX->current_cert. In release builds the accessor is
     * inlined, so we find the single instruction inside CertificateCallback that reads the
     * field and take the displacement straight out of it. That way a future BoringSSL layout
     * change is picked up automatically, rather than silently reading the wrong field.
     */
    function findCurrentCertOffset(platformPatterns, certificateCallbackAddr) {
        const patternInfo = platformPatterns[CURRENT_CERT_FIELD];

        const results = scanForSignature(
            certificateCallbackAddr,
            MAX_FUNCTION_BYTES_TO_SCAN,
            patternInfo.signatures
        );

        if (results.length !== 1) {
            throw new Error(`Found ${results.length} matches for ${CURRENT_CERT_FIELD} - expected exactly one`);
        }

        const instruction = Instruction.parse(asCode(results[0].address));
        const memoryOperand = instruction.operands.find(op => op.type === 'mem');

        if (!memoryOperand) {
            throw new Error(`No memory operand in ${CURRENT_CERT_FIELD} instruction: ${instruction}`);
        }

        const offset = memoryOperand.value.disp;
        if (!offset) {
            throw new Error(`Implausible ${CURRENT_CERT_FIELD} offset ${offset} from: ${instruction}`);
        }

        return offset;
    }

    /**
     * Resolves everything we need to hook, using one specific set of patterns. This has to
     * succeed or fail as a whole: a pattern set for the wrong engine build can match one
     * function by chance, and we want to fall through to the next set if it does, rather
     * than hooking a half-resolved mixture.
     */
    function resolveTargets(moduleRXRanges, patterns) {
        const certificateCallbackAddr = scanForFunction(moduleRXRanges, patterns, 'dart::bin::SSLCertContext::CertificateCallback');

        // Where the accessor still exists we call it; where LTO inlined it (all release
        // builds) we recover the field offset and read the store directly.
        let getCurrentCert;
        if (patterns[CURRENT_CERT_FIELD]) {
            const currentCertOffset = findCurrentCertOffset(patterns, certificateCallbackAddr);
            if (DEBUG_MODE) console.log(`X509_STORE_CTX->current_cert at +0x${currentCertOffset.toString(16)}`);
            getCurrentCert = (storeCtx) => storeCtx.add(currentCertOffset).readPointer();
        } else {
            const x509GetCurrentCert = new NativeFunction(
                asCode(scanForFunction(moduleRXRanges, patterns, 'X509_STORE_CTX_get_current_cert', certificateCallbackAddr)),
                'pointer',
                ['pointer']
            );
            getCurrentCert = (storeCtx) => x509GetCurrentCert(storeCtx);
        }

        // x509_to_buffer is just used as an anchor for searching:
        const x509ToBufferAddr = scanForAnchor(moduleRXRanges, patterns, 'bssl::x509_to_buffer');
        const i2d_X509 = new NativeFunction(
            asCode(scanForFunction(moduleRXRanges, patterns, 'i2d_X509', x509ToBufferAddr)),
            'int',
            ['pointer', 'pointer']
        );

        return { certificateCallbackAddr, getCurrentCert, i2d_X509 };
    }

    /** Works out which engine build is loaded, by seeing whose patterns actually match. */
    function findTargets(moduleRXRanges) {
        // Frida calls 32-bit x86 'ia32', but our patterns are keyed by the name Flutter
        // uses for the same architecture.
        const arch = Process.arch === 'ia32' ? 'x86' : Process.arch;
        const candidates = [`android-release/${arch}`, `android/${arch}`];

        for (const key of candidates) {
            if (!PATTERNS[key]) continue;

            try {
                const targets = resolveTargets(moduleRXRanges, PATTERNS[key]);
                if (DEBUG_MODE) console.log(`Matched Flutter ${key} patterns`);
                return targets;
            } catch (e) {
                // Expected for whichever engine build isn't loaded - we just try the next.
                if (DEBUG_MODE) console.log(`Flutter ${key} patterns don't apply here: ${e.message}`);
            }
        }

        throw new Error(`Could not match any known Flutter patterns for ${Process.arch}`);
    }

    function hookFlutter(moduleBase, moduleSize) {
        if (DEBUG_MODE) console.log('\n=== Disabling Flutter certificate pinning ===');

        const relevantRanges = Process.enumerateRanges('r-x').filter(range => {
            return range.base >= moduleBase && range.base < moduleBase.add(moduleSize);
        });

        try {
            const { certificateCallbackAddr, getCurrentCert, i2d_X509 } = findTargets(relevantRanges);

            // This callback is called for all TLS connections. It immediately returns 1 (success) if BoringSSL
            // trusts the cert, or it calls the configured BadCertificateCallback if it doesn't. Note that this
            // is called for every cert in the chain individually - not the whole chain at once.
            const dartCertificateCallback = new NativeFunction(
                asCode(certificateCallbackAddr),
                'int',
                ['int', 'pointer']
            );

            Interceptor.attach(dartCertificateCallback, {
                onEnter: function (args) {
                    this.x509Store = args[1];
                },
                onLeave: function (retval) {
                    if (retval.toInt32() === 1) return; // Ignore successful validations

                    // This certificate isn't trusted by BoringSSL or the app's certificate callback. Check it ourselves
                    // and override the result if it exactly matches our cert.
                    try {
                        const x509Cert = getCurrentCert(this.x509Store);

                        const derLength = i2d_X509(x509Cert, NULL);
                        if (derLength <= 0) {
                            throw new Error('Failed to get DER length for X509 cert');
                        }

                        // We create our own target buffer (rather than letting BoringSSL do so, which would
                        // require more hooks to handle cleanup).
                        const derBuffer = Memory.alloc(derLength)
                        const outPtr = Memory.alloc(Process.pointerSize);
                        outPtr.writePointer(derBuffer);

                        const certDataLength = i2d_X509(x509Cert, outPtr)
                        const certData = new Uint8Array(derBuffer.readByteArray(certDataLength));

                        if (certData.every((byte, j) => CERT_DER[j] === byte)) {
                            retval.replace(1); // We trust this certificate, return success
                        }
                    } catch (error) {
                        console.error('[!] Internal error in Flutter certificate unpinning:', error);
                    }
                }
            });

            console.log('=== Flutter certificate pinning disabled ===');
        } catch (error) {
            console.error('[!] Error preparing Flutter certificate pinning hooks:', error);
            throw error;
        }
    }

    let flutter = Process.findModuleByName('libflutter.so');
    if (flutter) {
        hookFlutter(flutter.base, flutter.size);
    } else {
        waitForModule('libflutter.so', function (module) {
            hookFlutter(module.base, module.size);
        });
    }
})();