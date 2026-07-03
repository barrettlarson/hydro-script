import { describe, expect, it } from "vitest";
import { pushSupported, urlBase64ToUint8Array } from "../push";

describe("urlBase64ToUint8Array", () => {
  it("decodes plain base64url", () => {
    expect(Array.from(urlBase64ToUint8Array("AQID"))).toEqual([1, 2, 3]);
  });

  it("restores stripped padding", () => {
    // "AQIDBA" is "AQIDBA==" with the padding VAPID keys omit
    expect(Array.from(urlBase64ToUint8Array("AQIDBA"))).toEqual([1, 2, 3, 4]);
  });

  it("maps url-safe characters back to standard base64", () => {
    // bytes 0xfb 0xef encode as "++8=" in standard base64, "--8" in base64url
    expect(Array.from(urlBase64ToUint8Array("--8"))).toEqual([0xfb, 0xef]);
    expect(Array.from(urlBase64ToUint8Array("__8"))).toEqual([0xff, 0xff]);
  });
});

describe("pushSupported", () => {
  it("is false in environments without a service worker (like jsdom)", () => {
    expect(pushSupported()).toBe(false);
  });
});
