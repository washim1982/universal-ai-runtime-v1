// The original UAR Java example (run with the SDK in sdks/java on the classpath).
import dev.uar.InferenceResponse;
import dev.uar.UARClient;

public class Quickstart {
    public static void main(String[] args) {
        UARClient client = new UARClient("http://localhost:9000");   // key from UAR_API_KEY

        InferenceResponse resp = client.inference(
            "local:default",
            "Explain quantum computing",
            "in_app_assistant"
        );

        System.out.println(resp.getText());
    }
}
