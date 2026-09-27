// Reference tool plugin (.NET): read-only statistics over a list of numbers, namespace "math".
using System.Text.Json.Nodes;
using Uar.Plugin;

var numbersSchema = JsonNode.Parse("""
    {"type": "object", "required": ["numbers"],
     "properties": {"numbers": {"type": "array", "items": {"type": "number"}, "minItems": 1, "maxItems": 10000}}}
    """)!.AsObject();

static double[] Numbers(JsonObject args) =>
    args["numbers"]?.AsArray().Select(n => n!.GetValue<double>()).ToArray()
    ?? throw new PluginException("numbers is required", "invalid_argument");

await PluginHost.ServeAsync(new PluginDefinition
{
    Id = "acme.mathutil",
    Version = "1.0.0",
    Kind = "tool",
    Tools =
    {
        new PluginTool("stats", "Count, sum, mean, median, min and max of a list of numbers.", numbersSchema, (args, ct) =>
        {
            var xs = Numbers(args);
            var sorted = xs.OrderBy(x => x).ToArray();
            var mid = sorted.Length / 2;
            var median = sorted.Length % 2 == 1 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
            return Task.FromResult(PluginReply.Of(new
            {
                count = xs.Length, sum = xs.Sum(), mean = xs.Average(), median, min = sorted[0], max = sorted[^1],
            }));
        }),
    },
}, args);
