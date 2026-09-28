// UAR inference sample (Java).
//
//   mvn -q compile exec:java -Dexec.args="Explain quantum computing in two sentences"
//
// Sign-in, first match wins:
//   UAR_CLIENT_ID + UAR_CLIENT_SECRET   a registered application: exchanged for an access token
//   UAR_API_KEY                         an API key
// Optional: UAR_URL (default http://127.0.0.1:9000), UAR_MODEL (default local:default).
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.uar.InferenceOptions;
import dev.uar.InferenceResponse;
import dev.uar.UARClient;
import dev.uar.UARException;
import dev.uar.UarEvent;

import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.stream.Stream;

public class Inference {
    static String env(String name, String def) {
        String v = System.getenv(name);
        return v == null || v.isEmpty() ? def : v;
    }

    /** Exchange an application's client credentials at the token service (OAuth 2.0 client_credentials). */
    static String accessToken(String baseUrl, String clientId, String secret) throws Exception {
        String form = "grant_type=client_credentials&client_id=" + URLEncoder.encode(clientId, StandardCharsets.UTF_8)
                + "&client_secret=" + URLEncoder.encode(secret, StandardCharsets.UTF_8);
        HttpRequest req = HttpRequest.newBuilder(URI.create(baseUrl + "/api/v1/oauth/token"))
                .header("Content-Type", "application/x-www-form-urlencoded")
                .POST(HttpRequest.BodyPublishers.ofString(form)).build();
        HttpResponse<String> resp = HttpClient.newBuilder().version(HttpClient.Version.HTTP_1_1).build()
                .send(req, HttpResponse.BodyHandlers.ofString());
        JsonNode body = new ObjectMapper().readTree(resp.body());
        if (!body.hasNonNull("access_token"))
            throw new IllegalStateException("token request failed (" + resp.statusCode() + "): "
                    + body.path("error").asText() + " " + body.path("error_description").asText());
        return body.get("access_token").asText();
    }

    public static void main(String[] args) throws Exception {
        String baseUrl = env("UAR_URL", "http://127.0.0.1:9000");
        String model = env("UAR_MODEL", "local:default");
        String prompt = args.length > 0 ? String.join(" ", args) : "Explain quantum computing in two sentences.";

        if (env("UAR_CLIENT_ID", "").isEmpty() && env("UAR_API_KEY", "").isEmpty()) {
            System.err.println("No credentials: set UAR_CLIENT_ID and UAR_CLIENT_SECRET (a registered application) or UAR_API_KEY. See samples/README.md.");
            System.exit(2);
        }
        UARClient client = new UARClient(baseUrl);   // picks up UAR_API_KEY
        String who = "API key";
        String clientId = System.getenv("UAR_CLIENT_ID");
        if (clientId != null && !clientId.isEmpty()) {
            client = new UARClient(baseUrl, null).withToken(accessToken(baseUrl, clientId, env("UAR_CLIENT_SECRET", "")));
            who = "application " + clientId;
        }
        System.out.printf("UAR %s | model %s | signed in with %s%n%n", baseUrl, model, who);
        InferenceOptions options = InferenceOptions.builder().maxTokens(300).build();

        try {
            // 1. One request, one complete answer.
            InferenceResponse resp = client.inference(model, prompt, options);
            System.out.printf("[%s/%s] %s%n", resp.getProvider(), resp.getModel(), resp.getText());
            JsonNode usage = resp.getUsage();
            System.out.printf("tokens: %s in, %s out%n%n", usage.path("input_tokens"), usage.path("output_tokens"));

            // 2. The same question, streamed token by token (closing the stream stops generation).
            System.out.print("streaming: ");
            try (Stream<UarEvent> events = client.stream(model, prompt, options)) {
                events.forEach(ev -> {
                    if (ev.getToken() != null) { System.out.print(ev.getToken()); System.out.flush(); }
                    else if ("error".equals(ev.getType()))
                        throw new IllegalStateException("stream error: " + ev.getBody().path("message").asText());
                });
            }
            System.out.println();
        } catch (UARException e) {   // e.g. 401 wrong credentials, 403 missing permission, 404 unknown model
            System.err.printf("UAR error %d %s: %s%n", e.getStatus(), e.getCode(), e.getMessage());
            System.exit(1);
        }
    }
}
