package com.lacedaemon.economy;

import org.bukkit.Bukkit;
import org.bukkit.Material;
import org.bukkit.OfflinePlayer;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.entity.PlayerDeathEvent;
import org.bukkit.event.player.PlayerInteractEvent;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerMoveEvent;
import org.bukkit.inventory.Inventory;
import org.bukkit.inventory.ItemStack;
import org.bukkit.plugin.java.JavaPlugin;

import java.io.IOException;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.EnumMap;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public final class LacedaemonEconomyPlugin extends JavaPlugin implements Listener {
    private record Commodity(String key, double ratio, boolean legacy) {}
    private record ApiResponse(int status, String body) {
        boolean ok() {
            return status >= 200 && status < 300;
        }
    }

    private static final Map<Material, Commodity> REGISTRY = new EnumMap<>(Material.class);
    private static final Map<String, Material> ALIASES = new HashMap<>();
    private static final Map<String, Material> DEFAULT_OUTPUT = new LinkedHashMap<>();
    private static final Map<String, Map<String, Material>> VARIANTS = new LinkedHashMap<>();
    private static final Pattern STRING_FIELD = Pattern.compile("\"%s\"\\s*:\\s*\"([^\"]*)\"");
    private static final Pattern NUMBER_FIELD = Pattern.compile("\"%s\"\\s*:\\s*(-?\\d+(?:\\.\\d+)?)");

    private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(8)).build();
    private final Map<UUID, Long> lastAliveReport = new HashMap<>();
    private String backendUrl;
    private String apiKey;

    static {
        registerCatalog();
    }

    @Override
    public void onEnable() {
        backendUrl = System.getenv().getOrDefault("BACKEND_URL", "http://localhost:8000/api").replaceAll("/+$", "");
        apiKey = System.getenv().getOrDefault("API_KEY", "").trim();
        if (apiKey.isEmpty()) {
            getLogger().severe("API_KEY is required. Disabling plugin.");
            Bukkit.getPluginManager().disablePlugin(this);
            return;
        }
        Bukkit.getPluginManager().registerEvents(this, this);
        getLogger().info("Lacedaemon Economy connected to " + backendUrl);
    }

    @EventHandler
    public void onJoin(PlayerJoinEvent event) {
        Player player = event.getPlayer();
        Bukkit.getScheduler().runTaskAsynchronously(this, () -> {
            post("/login/reward", "{\"mc_uuid\":\"" + player.getUniqueId() + "\"}");
            reportAlive(player, 0);
        });
    }

    @EventHandler
    public void onMove(PlayerMoveEvent event) {
        maybeReportAlive(event.getPlayer());
    }

    @EventHandler
    public void onInteract(PlayerInteractEvent event) {
        maybeReportAlive(event.getPlayer());
    }

    @EventHandler
    public void onDeath(PlayerDeathEvent event) {
        Player target = event.getEntity();
        Bukkit.getScheduler().runTaskAsynchronously(this, () -> {
            post("/alive/death", json(
                "mc_uuid", target.getUniqueId().toString(),
                "name", target.getName()
            ));
            Player killer = target.getKiller();
            if (killer != null && !killer.getUniqueId().equals(target.getUniqueId())) {
                ApiResponse response = post("/bounty/claim", json(
                    "target_uuid", target.getUniqueId().toString(),
                    "target_name", target.getName(),
                    "killer_uuid", killer.getUniqueId().toString(),
                    "killer_name", killer.getName()
                ));
                if (response.ok() && response.body.contains("\"status\":\"claimed\"")) {
                    String amount = number(response.body, "amount", "0");
                    Bukkit.getScheduler().runTask(this, () ->
                        killer.sendMessage("§6Bounty claimed: §e" + amount + " dragons")
                    );
                }
            }
        });
    }

    private void maybeReportAlive(Player player) {
        long now = System.currentTimeMillis();
        long last = lastAliveReport.getOrDefault(player.getUniqueId(), 0L);
        if (now - last < 60_000) {
            return;
        }
        lastAliveReport.put(player.getUniqueId(), now);
        double activeSeconds = Math.max(0, player.getTicksLived() / 20.0);
        Bukkit.getScheduler().runTaskAsynchronously(this, () -> reportAlive(player, activeSeconds));
    }

    private void reportAlive(Player player, double activeSeconds) {
        post("/alive/report", json(
            "mc_uuid", player.getUniqueId().toString(),
            "name", player.getName(),
            "active_seconds", activeSeconds
        ));
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!(sender instanceof Player player) && !command.getName().equalsIgnoreCase("helpmc")) {
            sender.sendMessage("Players only.");
            return true;
        }
        switch (command.getName().toLowerCase(Locale.ROOT)) {
            case "discord" -> handleDiscord((Player) sender);
            case "deposit" -> handleDeposit((Player) sender, args);
            case "withdraw" -> handleWithdraw((Player) sender, args);
            case "balance" -> handleBalance((Player) sender);
            case "itemgui" -> handleItemGui((Player) sender, args);
            case "alive" -> handleTextPage((Player) sender, "/alive/leaderboard_text", page(args), "Alive Leaderboard");
            case "bounties" -> handleTextPage((Player) sender, "/bounties_text", page(args), "Active Bounties");
            case "bounty", "bouny" -> handleBounty((Player) sender, args);
            case "helpmc" -> handleHelp(sender, args);
            default -> {
                return false;
            }
        }
        return true;
    }

    private void handleDiscord(Player player) {
        run(player, () -> {
            ApiResponse response = get("/link/generate?uuid=" + enc(player.getUniqueId().toString()));
            if (!response.ok()) {
                tell(player, "§cCould not create a link code.");
                return;
            }
            tell(player, "§6Discord link code: §f" + text(response.body, "code", "unknown"));
            tell(player, "§7Use §f/economy link §7in Discord.");
        });
    }

    private void handleDeposit(Player player, String[] args) {
        if (args.length == 1 && args[0].equalsIgnoreCase("inv")) {
            depositInventory(player);
            return;
        }
        if (args.length < 2) {
            player.sendMessage("§cUsage: §e/deposit <item> <amount|all> §7or §e/deposit inv");
            return;
        }
        if (args[0].equalsIgnoreCase("xp")) {
            int amount = args[1].equalsIgnoreCase("all") ? getTotalExperience(player) : parseInt(args[1], -1);
            if (amount <= 0 || getTotalExperience(player) < amount) {
                player.sendMessage("§cNot enough XP.");
                return;
            }
            run(player, () -> {
                ApiResponse response = post("/commodity/deposit", json("mc_uuid", player.getUniqueId().toString(), "commodity", "xp", "amount", amount));
                if (response.ok()) {
                    onMain(() -> {
                        setTotalExperience(player, getTotalExperience(player) - amount);
                        player.sendMessage("§aDeposited " + amount + " XP.");
                    });
                } else {
                    tell(player, "§cDeposit failed.");
                }
            });
            return;
        }
        Material material = resolveMaterial(args[0], args.length >= 3 ? args[2] : null);
        if (material == null || !REGISTRY.containsKey(material)) {
            player.sendMessage("§cUnknown item. Try §e/helpmc§c.");
            return;
        }
        int owned = count(player.getInventory(), material);
        int amount = args[1].equalsIgnoreCase("all") ? owned : parseInt(args[1], -1);
        if (amount <= 0 || owned < amount) {
            player.sendMessage("§cYou do not have enough " + pretty(material) + ".");
            return;
        }
        Commodity commodity = REGISTRY.get(material);
        double baseAmount = amount * commodity.ratio();
        run(player, () -> {
            ApiResponse response = postDeposit(player.getUniqueId(), commodity, baseAmount);
            if (response.ok()) {
                logDeposit(player.getUniqueId(), "deposit", commodity.key(), amount, baseAmount);
                onMain(() -> {
                    remove(player.getInventory(), material, amount);
                    player.sendMessage("§aDeposited " + amount + " " + pretty(material) + ".");
                });
            } else {
                tell(player, "§cDeposit failed.");
            }
        });
    }

    private void depositInventory(Player player) {
        Map<Material, Integer> totals = new LinkedHashMap<>();
        for (ItemStack stack : player.getInventory().getContents()) {
            if (stack != null && REGISTRY.containsKey(stack.getType())) {
                totals.merge(stack.getType(), stack.getAmount(), Integer::sum);
            }
        }
        if (totals.isEmpty()) {
            player.sendMessage("§7No depositable items found.");
            return;
        }
        run(player, () -> {
            Map<Material, Integer> movedByMaterial = new LinkedHashMap<>();
            int moved = 0;
            for (Map.Entry<Material, Integer> entry : totals.entrySet()) {
                Commodity commodity = REGISTRY.get(entry.getKey());
                double baseAmount = entry.getValue() * commodity.ratio();
                ApiResponse response = postDeposit(player.getUniqueId(), commodity, baseAmount);
                if (response.ok()) {
                    movedByMaterial.merge(entry.getKey(), entry.getValue(), Integer::sum);
                    logDeposit(player.getUniqueId(), "deposit", commodity.key(), entry.getValue(), baseAmount);
                    moved += entry.getValue();
                }
            }
            int finalMoved = moved;
            onMain(() -> {
                movedByMaterial.forEach((material, amount) -> remove(player.getInventory(), material, amount));
                player.sendMessage("§aDeposited " + finalMoved + " item(s).");
            });
        });
    }

    private void handleWithdraw(Player player, String[] args) {
        if (args.length < 2) {
            player.sendMessage("§cUsage: §e/withdraw <item> <amount> [variant]");
            return;
        }
        if (args[0].equalsIgnoreCase("xp")) {
            int amount = parseInt(args[1], -1);
            if (amount <= 0) {
                player.sendMessage("§cAmount must be positive.");
                return;
            }
            run(player, () -> {
                ApiResponse response = post("/commodity/withdraw", json("mc_uuid", player.getUniqueId().toString(), "commodity", "xp", "amount", amount));
                if (response.ok()) {
                    onMain(() -> {
                        setTotalExperience(player, getTotalExperience(player) + amount);
                        player.sendMessage("§aWithdrew " + amount + " XP.");
                    });
                } else {
                    tell(player, "§cWithdraw failed: " + detail(response.body));
                }
            });
            return;
        }
        Material material = resolveMaterial(args[0], args.length >= 3 ? args[2] : null);
        if (material == null || !REGISTRY.containsKey(material)) {
            player.sendMessage("§cUnknown item. Try §e/helpmc§c.");
            return;
        }
        int amount = parseInt(args[1], -1);
        if (amount <= 0) {
            player.sendMessage("§cAmount must be positive.");
            return;
        }
        Commodity commodity = REGISTRY.get(material);
        double baseAmount = amount * commodity.ratio();
        run(player, () -> {
            ApiResponse response = postWithdraw(player.getUniqueId(), commodity, baseAmount);
            if (response.ok()) {
                logDeposit(player.getUniqueId(), "withdraw", commodity.key(), amount, baseAmount);
                onMain(() -> {
                    give(player.getInventory(), material, amount);
                    player.sendMessage("§aWithdrew " + amount + " " + pretty(material) + ".");
                });
            } else {
                tell(player, "§cWithdraw failed: " + detail(response.body));
            }
        });
    }

    private void handleBalance(Player player) {
        run(player, () -> {
            ApiResponse legacy = get("/balance/" + enc(player.getUniqueId().toString()));
            ApiResponse commodities = get("/commodity/balance/" + enc(player.getUniqueId().toString()));
            if (!legacy.ok()) {
                tell(player, "§cCould not load balance.");
                return;
            }
            tell(player, "§6Economy++ Vault");
            tell(player, "§eDragons: §f" + number(legacy.body, "mdragons", "0") + " §7(locked " + number(legacy.body, "mdragons_locked", "0") + ")");
            tell(player, "§eNetherite: §f" + number(legacy.body, "netherite", "0"));
            tell(player, "§eDiamond: §f" + number(legacy.body, "diamond", "0"));
            if (commodities.ok()) {
                tell(player, "§7Commodities: " + commodities.body.replaceAll("[{}\"\\\\]", ""));
            }
        });
    }

    private void handleItemGui(Player player, String[] args) {
        if (args.length < 1) {
            player.sendMessage("§cUsage: §e/itemgui <item>");
            return;
        }
        run(player, () -> {
            ApiResponse response = get("/inventory/" + enc(player.getUniqueId().toString()) + "/" + enc(args[0]));
            if (!response.ok()) {
                tell(player, "§cCould not load item balance.");
                return;
            }
            tell(player, "§6" + args[0] + " balance");
            tell(player, "§eVault: §f" + number(response.body, "vault", "0"));
            tell(player, "§eIn market: §f" + number(response.body, "in_orders", "0"));
            tell(player, "§eTotal: §f" + number(response.body, "total", "0"));
        });
    }

    private void handleTextPage(Player player, String endpoint, int page, String title) {
        int offset = Math.max(0, page - 1) * 10;
        run(player, () -> {
            ApiResponse response = get(endpoint + "?limit=10&offset=" + offset);
            tell(player, "§6" + title + " §7(page " + page + ")");
            tell(player, response.ok() ? response.body : "§cCould not load page.");
        });
    }

    private void handleBounty(Player player, String[] args) {
        if (args.length < 2) {
            player.sendMessage("§cUsage: §e/bounty <player> <dragons>");
            return;
        }
        OfflinePlayer target = Bukkit.getOfflinePlayer(args[0]);
        int amount = parseInt(args[1], -1);
        if (amount <= 0) {
            player.sendMessage("§cAmount must be positive.");
            return;
        }
        run(player, () -> {
            ApiResponse response = post("/bounty/place", json(
                "issuer_uuid", player.getUniqueId().toString(),
                "target_uuid", target.getUniqueId().toString(),
                "target_name", target.getName() == null ? args[0] : target.getName(),
                "amount", amount
            ));
            tell(player, response.ok() ? "§aBounty placed." : "§cBounty failed: " + detail(response.body));
        });
    }

    private void handleHelp(CommandSender sender, String[] args) {
        sender.sendMessage("§6Economy++ items");
        sender.sendMessage("§eOres: §fcoal, iron, gold, copper, emerald, redstone, lapis, diamond, netherite");
        sender.sendMessage("§eBlocks: §fstone, cobblestone, deepslate, blackstone, basalt, sand, gravel, clay, glass, obsidian, ice, dirt");
        sender.sendMessage("§eWood: §foverworld_log <oak|spruce|birch|jungle|acacia|dark_oak|mangrove|cherry>, nether_log <crimson|warped>");
        sender.sendMessage("§eColor: §fwool/concrete/concrete_powder <color>, color_dye");
        sender.sendMessage("§eExamples: §f/deposit iron_ingot all §7| §f/withdraw overworld_log 32 oak §7| §f/deposit inv");
    }

    private ApiResponse postDeposit(UUID uuid, Commodity commodity, double amount) {
        if (commodity.legacy()) {
            String item = commodity.key().equals("diamond") ? "DIAMOND" : "NETHERITE_INGOT";
            return post("/deposit", json("uuid", uuid.toString(), "item", item, "amount", amount));
        }
        return post("/commodity/deposit", json("mc_uuid", uuid.toString(), "commodity", commodity.key(), "amount", amount));
    }

    private ApiResponse postWithdraw(UUID uuid, Commodity commodity, double amount) {
        if (commodity.legacy()) {
            String item = commodity.key().equals("diamond") ? "DIAMOND" : "NETHERITE_INGOT";
            return post("/withdraw", json("uuid", uuid.toString(), "item", item, "amount", amount));
        }
        return post("/commodity/withdraw", json("mc_uuid", uuid.toString(), "commodity", commodity.key(), "amount", amount));
    }

    private void logDeposit(UUID uuid, String action, String item, double amount, double baseUnits) {
        post("/log/deposit_withdraw", json(
            "mc_uuid", uuid.toString(),
            "action", action,
            "item", item,
            "amount", amount,
            "base_units", baseUnits
        ));
    }

    private ApiResponse get(String path) {
        return request("GET", path, null);
    }

    private ApiResponse post(String path, String body) {
        return request("POST", path, body);
    }

    private ApiResponse request(String method, String path, String body) {
        try {
            HttpRequest.Builder builder = HttpRequest.newBuilder(URI.create(backendUrl + path))
                .timeout(Duration.ofSeconds(15))
                .header("X-API-Key", apiKey);
            if (body == null) {
                builder.method(method, HttpRequest.BodyPublishers.noBody());
            } else {
                builder.header("Content-Type", "application/json")
                    .method(method, HttpRequest.BodyPublishers.ofString(body));
            }
            HttpResponse<String> response = http.send(builder.build(), HttpResponse.BodyHandlers.ofString());
            return new ApiResponse(response.statusCode(), response.body());
        } catch (IOException | InterruptedException ex) {
            if (ex instanceof InterruptedException) {
                Thread.currentThread().interrupt();
            }
            return new ApiResponse(599, "{\"detail\":\"" + esc(ex.getMessage()) + "\"}");
        }
    }

    private void run(Player player, Runnable task) {
        Bukkit.getScheduler().runTaskAsynchronously(this, () -> {
            try {
                task.run();
            } catch (Exception ex) {
                getLogger().warning(ex.getMessage());
                Bukkit.getScheduler().runTask(this, () -> player.sendMessage("§cEconomy request failed."));
            }
        });
    }

    private void tell(Player player, String message) {
        onMain(() -> player.sendMessage(message));
    }

    private void onMain(Runnable action) {
        if (Bukkit.isPrimaryThread()) {
            action.run();
        } else {
            Bukkit.getScheduler().runTask(this, action);
        }
    }

    private static void registerCatalog() {
        reg(Material.COAL_BLOCK, "coal", 1, false); reg(Material.COAL, "coal", 1.0 / 9, false);
        reg(Material.IRON_BLOCK, "iron", 1, false); reg(Material.IRON_INGOT, "iron", 1.0 / 9, false);
        reg(Material.GOLD_BLOCK, "gold", 1, false); reg(Material.GOLD_INGOT, "gold", 1.0 / 9, false); reg(Material.GOLD_NUGGET, "gold", 1.0 / 81, false);
        reg(Material.COPPER_BLOCK, "copper", 9, false); reg(Material.COPPER_INGOT, "copper", 1, false);
        reg(Material.EMERALD_BLOCK, "emerald", 1, false); reg(Material.EMERALD, "emerald", 1.0 / 9, false);
        reg(Material.REDSTONE_BLOCK, "redstone", 9, false); reg(Material.REDSTONE, "redstone", 1, false);
        reg(Material.LAPIS_BLOCK, "lapis", 9, false); reg(Material.LAPIS_LAZULI, "lapis", 1, false);
        reg(Material.DIAMOND, "diamond", 1, true); reg(Material.DIAMOND_BLOCK, "diamond", 9, true);
        reg(Material.NETHERITE_INGOT, "netherite", 1, true);

        for (Material m : List.of(Material.STONE, Material.COBBLESTONE, Material.DEEPSLATE, Material.COBBLED_DEEPSLATE, Material.BLACKSTONE, Material.BASALT, Material.NETHERRACK, Material.SOUL_SAND, Material.SOUL_SOIL, Material.QUARTZ, Material.NETHER_WART, Material.END_STONE, Material.CHORUS_FRUIT, Material.POPPED_CHORUS_FRUIT, Material.DRAGON_BREATH, Material.SAND, Material.RED_SAND, Material.GRAVEL, Material.GLASS, Material.OBSIDIAN, Material.DIRT)) {
            reg(m, key(m), 1, false);
        }
        reg(Material.NETHER_BRICKS, "nether_brick_block", 1, false); reg(Material.NETHER_BRICK, "nether_brick_block", 0.25, false);
        reg(Material.GLOWSTONE, "glowstone", 1, false); reg(Material.GLOWSTONE_DUST, "glowstone", 0.25, false);
        reg(Material.CLAY, "clay", 4, false); reg(Material.CLAY_BALL, "clay", 1, false); reg(Material.BRICKS, "clay", 4, false); reg(Material.BRICK, "clay", 1, false);
        reg(Material.ICE, "ice", 1, false); reg(Material.PACKED_ICE, "ice", 9, false); reg(Material.BLUE_ICE, "ice", 81, false);
        reg(Material.WHEAT, "wheat", 1, false); reg(Material.BREAD, "wheat", 3, false);
        for (Material m : List.of(Material.CARROT, Material.POTATO, Material.BEETROOT, Material.PUMPKIN, Material.BAMBOO, Material.CACTUS, Material.COCOA_BEANS, Material.ROTTEN_FLESH, Material.BONE, Material.STRING, Material.GUNPOWDER, Material.SPIDER_EYE, Material.ENDER_PEARL, Material.SLIME_BALL, Material.LEATHER, Material.ARROW, Material.BLAZE_ROD, Material.GHAST_TEAR, Material.MAGMA_CREAM, Material.SHULKER_SHELL, Material.TOTEM_OF_UNDYING, Material.WITHER_SKELETON_SKULL, Material.NETHER_STAR, Material.FEATHER, Material.INK_SAC, Material.GLOW_INK_SAC)) {
            reg(m, key(m), 1, false);
        }
        reg(Material.MELON_SLICE, "melon", 1, false); reg(Material.MELON, "melon", 9, false);
        reg(Material.SUGAR_CANE, "sugar_cane", 1, false); reg(Material.PAPER, "sugar_cane", 1, false);

        variants("overworld_log", Map.of(
            "oak", Material.OAK_LOG, "spruce", Material.SPRUCE_LOG, "birch", Material.BIRCH_LOG, "jungle", Material.JUNGLE_LOG,
            "acacia", Material.ACACIA_LOG, "dark_oak", Material.DARK_OAK_LOG, "mangrove", Material.MANGROVE_LOG, "cherry", Material.CHERRY_LOG
        ));
        variants("nether_log", Map.of("crimson", Material.CRIMSON_STEM, "warped", Material.WARPED_STEM));
        variants("wool", colorMap("_WOOL"));
        variants("concrete", colorMap("_CONCRETE"));
        variants("concrete_powder", colorMap("_CONCRETE_POWDER"));
        for (String color : colors()) {
            Material dye = Material.matchMaterial(color.toUpperCase(Locale.ROOT) + "_DYE");
            if (dye != null) {
                reg(dye, color + "_dye", 1, false);
            }
        }

        alias("coal", Material.COAL_BLOCK); alias("iron", Material.IRON_BLOCK); alias("gold", Material.GOLD_BLOCK);
        alias("copper", Material.COPPER_INGOT); alias("emerald", Material.EMERALD_BLOCK); alias("lapis", Material.LAPIS_LAZULI);
        alias("diamond", Material.DIAMOND); alias("netherite", Material.NETHERITE_INGOT);
        alias("log", Material.OAK_LOG); alias("wood", Material.OAK_LOG); alias("stem", Material.CRIMSON_STEM);
        alias("glow_dust", Material.GLOWSTONE_DUST); alias("glow_ink", Material.GLOW_INK_SAC);
    }

    private static void reg(Material material, String key, double ratio, boolean legacy) {
        REGISTRY.put(material, new Commodity(key, ratio, legacy));
        DEFAULT_OUTPUT.putIfAbsent(key, material);
        ALIASES.putIfAbsent(key, material);
        ALIASES.put(material.name().toLowerCase(Locale.ROOT), material);
    }

    private static void variants(String key, Map<String, Material> values) {
        VARIANTS.put(key, values);
        for (Material material : values.values()) {
            reg(material, key, 1, false);
        }
    }

    private static Map<String, Material> colorMap(String suffix) {
        Map<String, Material> out = new LinkedHashMap<>();
        for (String color : colors()) {
            Material material = Material.matchMaterial(color.toUpperCase(Locale.ROOT) + suffix);
            if (material != null) {
                out.put(color, material);
            }
        }
        return out;
    }

    private static List<String> colors() {
        return List.of("white", "orange", "magenta", "light_blue", "yellow", "lime", "pink", "gray", "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black");
    }

    private Material resolveMaterial(String raw, String variant) {
        String key = raw.toLowerCase(Locale.ROOT).replace('-', '_');
        if (variant != null && VARIANTS.containsKey(key)) {
            return VARIANTS.get(key).get(variant.toLowerCase(Locale.ROOT).replace('-', '_'));
        }
        return ALIASES.getOrDefault(key, Material.matchMaterial(key.toUpperCase(Locale.ROOT)));
    }

    private static void alias(String key, Material material) {
        ALIASES.put(key, material);
    }

    private static String key(Material material) {
        return material.name().toLowerCase(Locale.ROOT);
    }

    private static int count(Inventory inv, Material material) {
        int total = 0;
        for (ItemStack stack : inv.getContents()) {
            if (stack != null && stack.getType() == material) {
                total += stack.getAmount();
            }
        }
        return total;
    }

    private static void remove(Inventory inv, Material material, int amount) {
        int left = amount;
        for (ItemStack stack : inv.getContents()) {
            if (stack == null || stack.getType() != material) {
                continue;
            }
            int take = Math.min(left, stack.getAmount());
            stack.setAmount(stack.getAmount() - take);
            left -= take;
            if (left <= 0) {
                return;
            }
        }
    }

    private static void give(Inventory inv, Material material, int amount) {
        int left = amount;
        while (left > 0) {
            int stack = Math.min(left, material.getMaxStackSize());
            inv.addItem(new ItemStack(material, stack));
            left -= stack;
        }
    }

    private static int getTotalExperience(Player player) {
        int level = player.getLevel();
        float progress = player.getExp();
        int total = Math.round(expAtLevel(level) + progress * expToNext(level));
        return Math.max(total, player.getTotalExperience());
    }

    private static void setTotalExperience(Player player, int amount) {
        player.setExp(0);
        player.setLevel(0);
        player.setTotalExperience(0);
        player.giveExp(Math.max(0, amount));
    }

    private static int expAtLevel(int level) {
        if (level <= 16) return level * level + 6 * level;
        if (level <= 31) return (int) (2.5 * level * level - 40.5 * level + 360);
        return (int) (4.5 * level * level - 162.5 * level + 2220);
    }

    private static int expToNext(int level) {
        if (level <= 15) return 2 * level + 7;
        if (level <= 30) return 5 * level - 38;
        return 9 * level - 158;
    }

    private static int page(String[] args) {
        return args.length == 0 ? 1 : Math.max(1, parseInt(args[0], 1));
    }

    private static int parseInt(String raw, int fallback) {
        try {
            return Integer.parseInt(raw.replace(",", ""));
        } catch (NumberFormatException ex) {
            return fallback;
        }
    }

    private static String pretty(Material material) {
        return material.name().toLowerCase(Locale.ROOT).replace('_', ' ');
    }

    private static String text(String json, String field, String fallback) {
        Matcher matcher = Pattern.compile(String.format(STRING_FIELD.pattern(), Pattern.quote(field))).matcher(json);
        return matcher.find() ? matcher.group(1) : fallback;
    }

    private static String number(String json, String field, String fallback) {
        Matcher matcher = Pattern.compile(String.format(NUMBER_FIELD.pattern(), Pattern.quote(field))).matcher(json);
        return matcher.find() ? matcher.group(1) : fallback;
    }

    private static String detail(String json) {
        return text(json, "detail", "backend rejected the request");
    }

    private static String json(Object... pairs) {
        List<String> fields = new ArrayList<>();
        for (int i = 0; i < pairs.length; i += 2) {
            String key = String.valueOf(pairs[i]);
            Object value = pairs[i + 1];
            if (value instanceof Number || value instanceof Boolean) {
                fields.add("\"" + esc(key) + "\":" + value);
            } else {
                fields.add("\"" + esc(key) + "\":\"" + esc(String.valueOf(value)) + "\"");
            }
        }
        return "{" + String.join(",", fields) + "}";
    }

    private static String esc(String raw) {
        return raw == null ? "" : raw.replace("\\", "\\\\").replace("\"", "\\\"");
    }

    private static String enc(String raw) {
        return URLEncoder.encode(raw, StandardCharsets.UTF_8);
    }
}
