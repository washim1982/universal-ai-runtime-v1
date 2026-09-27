package dev.uar.example;

import dev.uar.plugin.UarPlugin;
import dev.uar.plugin.UarPlugin.PluginException;
import dev.uar.plugin.UarPlugin.Reply;

import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.stream.Collectors;

/** Reference executable agent plugin (Java): the most frequent words of a text, ignoring stop words. */
public final class WordFrequency {
    private static final Set<String> STOP = Set.of("the", "a", "an", "and", "or", "of", "to", "in", "is", "it", "for", "on");
    private static int top = 3;

    public static void main(String[] args) throws Exception {
        UarPlugin.serve(UarPlugin.definition("acme.wordfreq", "1.0.0", "agent")
                .init((config, secrets) -> {
                    Object n = config.get("top");
                    if (n instanceof Number num) top = num.intValue();
                })
                .agent((agent, input) -> {
                    if (!"word_frequency".equals(agent)) throw new PluginException("unknown agent " + agent, "not_found");
                    Object text = input.get("text");
                    if (!(text instanceof String s) || s.isBlank()) throw new PluginException("input.text is required", "invalid_argument");
                    Map<String, Long> counts = new TreeMap<>();
                    for (String w : s.toLowerCase(Locale.ROOT).split("[^\\p{L}\\p{N}']+")) {
                        if (!w.isEmpty() && !STOP.contains(w)) counts.merge(w, 1L, Long::sum);
                    }
                    List<Map<String, Object>> ranked = counts.entrySet().stream()
                            .sorted(Map.Entry.<String, Long>comparingByValue(Comparator.reverseOrder()).thenComparing(Map.Entry.comparingByKey()))
                            .limit(top)
                            .map(e -> { Map<String, Object> m = new LinkedHashMap<>(); m.put("word", e.getKey()); m.put("count", e.getValue()); return m; })
                            .collect(Collectors.toList());
                    Map<String, Object> out = new LinkedHashMap<>();
                    out.put("top", ranked);
                    out.put("distinct", counts.size());
                    return Reply.output(out);
                })
                .build());
    }

    private WordFrequency() {}
}
