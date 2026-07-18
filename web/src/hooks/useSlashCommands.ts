import { useCallback, useMemo, useState } from "react";

export interface SlashCommand {
  name: string;
  description: string;
  usage?: string;
  /** Handler: return true if the command was handled (consumes the input). */
  handler: (args: string) => boolean | Promise<boolean>;
}

export function useSlashCommands() {
  const [showMenu, setShowMenu] = useState(false);
  const [matchPrefix, setMatchPrefix] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);

  // ── Register commands ────────────────────────────────────────────────
  // Callers inject their handlers via this ref-like pattern.
  const [commands, setCommands] = useState<SlashCommand[]>([]);

  const registerCommands = useCallback((cmds: SlashCommand[]) => {
    setCommands(cmds);
  }, []);

  const filtered = useMemo(() => {
    if (!matchPrefix) return commands;
    return commands.filter((c) =>
      c.name.startsWith(matchPrefix.toLowerCase()),
    );
  }, [commands, matchPrefix]);

  // ── Input handling ───────────────────────────────────────────────────
  const handleInputChange = useCallback(
    (value: string) => {
      const trimmed = value.trimStart();
      if (trimmed.startsWith("/")) {
        const afterSlash = trimmed.slice(1);
        const word = afterSlash.split(/\s+/)[0] || "";
        setMatchPrefix(word);
        setShowMenu(word.length > 0 || trimmed === "/");
        setSelectedIndex(0);
      } else {
        setShowMenu(false);
        setMatchPrefix("");
      }
    },
    [],
  );

  const selectCommand = useCallback(
    (cmd: SlashCommand) => {
      setShowMenu(false);
      setMatchPrefix("");
      return cmd;
    },
    [],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent, currentValue: string): boolean => {
      if (!showMenu || filtered.length === 0) return false;

      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSelectedIndex((i) => (i + 1) % filtered.length);
        return true;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSelectedIndex((i) => (i - 1 + filtered.length) % filtered.length);
        return true;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        const cmd = filtered[selectedIndex];
        if (cmd) {
          selectCommand(cmd);
          // Execute the command with the rest of the input after the command name
          const afterSlash = currentValue.trimStart().slice(1);
          const rest = afterSlash.slice(cmd.name.length).trim();
          cmd.handler(rest);
          return true;
        }
      }
      if (e.key === "Escape") {
        setShowMenu(false);
        return true;
      }
      return false;
    },
    [showMenu, filtered, selectedIndex, selectCommand],
  );

  return {
    showMenu,
    filtered,
    selectedIndex,
    matchPrefix,
    handleInputChange,
    handleKeyDown,
    selectCommand,
    registerCommands,
    setShowMenu,
  };
}
