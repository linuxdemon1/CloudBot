import asyncio
import importlib
import logging
import sys
from collections import defaultdict
from functools import partial
from itertools import chain
from operator import attrgetter
from pathlib import Path
from weakref import WeakValueDictionary

from .event import Event, PostHookEvent
from .plugin import Plugin
from .util import async_util
from .util.func_utils import call_with_args

logger = logging.getLogger("cloudbot")


class PluginManager:
    """
    PluginManager is the core of CloudBot plugin loading.

    PluginManager loads Plugins, and adds their Hooks to easy-access dicts/lists.

    Each Plugin represents a file, and loads hooks onto itself using find_hooks.

    Plugins are the lowest level of abstraction in this class. There are four different plugin types:
    - CommandPlugin is for bot commands
    - RawPlugin hooks onto irc_raw irc lines
    - RegexPlugin loads a regex parameter, and executes on irc lines which match the regex
    - SievePlugin is a catch-all sieve, which all other plugins go through before being executed.

    :type bot: cloudbot.bot.CloudBot
    :type plugins: dict[str, Plugin]
    :type commands: dict[str, cloudbot.hooks.command.CommandHook]
    :type raw_triggers: dict[str, list[cloudbot.hooks.raw.RawHook]]
    :type catch_all_triggers: list[cloudbot.hooks.raw.RawHook]
    :type event_type_hooks: dict[cloudbot.event.EventType, list[cloudbot.hooks.event.EventHook]]
    :type regex_hooks: list[(re.__Regex, cloudbot.hooks.regex.RegexHook)]
    :type sieves: list[cloudbot.hooks.sieve.SieveHook]
    """

    def __init__(self, bot):
        """
        Creates a new PluginManager. You generally only need to do this from inside cloudbot.bot.CloudBot
        :type bot: cloudbot.bot.CloudBot
        """
        self.bot = bot

        self.plugins = {}
        self._plugin_name_map = WeakValueDictionary()
        self.commands = {}
        self.raw_triggers = {}
        self.catch_all_triggers = []
        self.event_type_hooks = {}
        self.regex_hooks = []
        self.sieves = []
        self.cap_hooks = {"on_available": defaultdict(list), "on_ack": defaultdict(list)}
        self.connect_hooks = []
        self.out_sieves = []
        self.hook_hooks = defaultdict(list)
        self.perm_hooks = defaultdict(list)
        self._hook_waiting_queues = {}

    def find_plugin(self, title):
        """
        Finds a loaded plugin and returns its Plugin object
        :param title: the title of the plugin to find
        :return: The Plugin object if it exists, otherwise None
        """
        return self._plugin_name_map.get(title)

    async def load_all(self, plugin_dir):
        """
        Load a plugin from each *.py file in the given directory.

        Won't load any plugins listed in "disabled_plugins".

        :type plugin_dir: str
        """
        plugin_dir = Path(plugin_dir)
        # Load all .py files in the plugins directory and any subdirectory
        # But ignore files starting with _
        path_list = plugin_dir.rglob("[!_]*.py")
        # Load plugins asynchronously :O
        await asyncio.gather(*[self.load_plugin(path) for path in path_list], loop=self.bot.loop)

    async def unload_all(self):
        await asyncio.gather(
            *[self.unload_plugin(path) for path in self.plugins], loop=self.bot.loop
        )

    def should_load(self, title, noisy=False):
        if 'plugin_loading' not in self.bot.config:
            return True

        pl = self.bot.config.get("plugin_loading")

        if pl.get("use_whitelist", False):
            if title not in pl.get("whitelist", []):
                if noisy:
                    logger.info('Not loading plugin module "%s": plugin not whitelisted', title)

                return False
        else:
            if title in pl.get("blacklist", []):
                if noisy:
                    logger.info('Not loading plugin module "%s": plugin blacklisted', title)

                return False

        return True

    async def load_plugin(self, path):
        """
        Loads a plugin from the given path and plugin object, then registers all hooks from that plugin.

        Won't load any plugins listed in "disabled_plugins".

        :type path: str | Path
        """

        path = Path(path)
        file_path = path.resolve()
        file_name = file_path.name
        # Resolve the path relative to the current directory
        plugin_path = file_path.relative_to(self.bot.base_dir)
        title = '.'.join(plugin_path.parts[1:]).rsplit('.', 1)[0]

        if not self.should_load(title, True):
            return

        # make sure to unload the previously loaded plugin from this path, if it was loaded.
        if str(file_path) in self.plugins:
            await self.unload_plugin(file_path)

        module_name = "plugins.{}".format(title)
        try:
            plugin_module = importlib.import_module(module_name)
            # if this plugin was loaded before, reload it
            if hasattr(plugin_module, "_cloudbot_loaded"):
                importlib.reload(plugin_module)
        except Exception:
            logger.exception("Error loading %s:", title)
            return

        # create the plugin
        plugin = Plugin(str(file_path), file_name, title, plugin_module)

        # proceed to register hooks

        # create database tables
        await plugin.create_tables(self.bot)

        # run on_start hooks
        for on_start_hook in plugin.hooks["on_start"]:
            success = await self.launch(on_start_hook, Event(bot=self.bot, hook=on_start_hook))
            if not success:
                logger.warning("Not registering hooks from plugin %s: on_start hook errored", plugin.title)

                # unregister databases
                plugin.unregister_tables(self.bot)
                return

        self.plugins[plugin.file_path] = plugin
        self._plugin_name_map[plugin.title] = plugin

        for on_cap_available_hook in plugin.hooks["on_cap_available"]:
            for cap in on_cap_available_hook.caps:
                self.cap_hooks["on_available"][cap.casefold()].append(on_cap_available_hook)
            self._log_hook(on_cap_available_hook)

        for on_cap_ack_hook in plugin.hooks["on_cap_ack"]:
            for cap in on_cap_ack_hook.caps:
                self.cap_hooks["on_ack"][cap.casefold()].append(on_cap_ack_hook)
            self._log_hook(on_cap_ack_hook)

        for periodic_hook in plugin.hooks["periodic"]:
            task = async_util.wrap_future(self._start_periodic(periodic_hook))
            plugin.tasks.append(task)
            self._log_hook(periodic_hook)

        # register commands
        for command_hook in plugin.hooks["command"]:
            for alias in command_hook.aliases:
                if alias in self.commands:
                    logger.warning(
                        "Plugin %s attempted to register command %s which was "
                        "already registered by %s. Ignoring new assignment.",
                        plugin.title, alias, self.commands[alias].plugin.title
                    )
                else:
                    self.commands[alias] = command_hook
            self._log_hook(command_hook)

        # register raw hooks
        for raw_hook in plugin.hooks["irc_raw"]:
            if raw_hook.is_catch_all():
                self.catch_all_triggers.append(raw_hook)
            else:
                for trigger in raw_hook.triggers:
                    if trigger in self.raw_triggers:
                        self.raw_triggers[trigger].append(raw_hook)
                    else:
                        self.raw_triggers[trigger] = [raw_hook]
            self._log_hook(raw_hook)

        # register events
        for event_hook in plugin.hooks["event"]:
            for event_type in event_hook.types:
                if event_type in self.event_type_hooks:
                    self.event_type_hooks[event_type].append(event_hook)
                else:
                    self.event_type_hooks[event_type] = [event_hook]
            self._log_hook(event_hook)

        # register regexps
        for regex_hook in plugin.hooks["regex"]:
            for regex_match in regex_hook.regexes:
                self.regex_hooks.append((regex_match, regex_hook))
            self._log_hook(regex_hook)

        # register sieves
        for sieve_hook in plugin.hooks["sieve"]:
            self.sieves.append(sieve_hook)
            self._log_hook(sieve_hook)

        # register connect hooks
        for connect_hook in plugin.hooks["on_connect"]:
            self.connect_hooks.append(connect_hook)
            self._log_hook(connect_hook)

        for out_hook in plugin.hooks["irc_out"]:
            self.out_sieves.append(out_hook)
            self._log_hook(out_hook)

        for post_hook in plugin.hooks["post_hook"]:
            self.hook_hooks["post"].append(post_hook)
            self._log_hook(post_hook)

        for perm_hook in plugin.hooks["perm_check"]:
            for perm in perm_hook.perms:
                self.perm_hooks[perm].append(perm_hook)

            self._log_hook(perm_hook)

        # sort sieve hooks by priority
        self.sieves.sort(key=lambda x: x.priority)
        self.connect_hooks.sort(key=attrgetter("priority"))

        # Sort hooks
        self.regex_hooks.sort(key=lambda x: x[1].priority)
        dicts_of_lists_of_hooks = (self.event_type_hooks, self.raw_triggers, self.perm_hooks, self.hook_hooks)
        lists_of_hooks = [self.catch_all_triggers, self.sieves, self.connect_hooks, self.out_sieves]
        lists_of_hooks.extend(chain.from_iterable(d.values() for d in dicts_of_lists_of_hooks))

        for lst in lists_of_hooks:
            lst.sort(key=attrgetter("priority"))

        # we don't need this anymore
        del plugin.hooks["on_start"]

    async def unload_plugin(self, path):
        """
        Unloads the plugin from the given path, unregistering all hooks from the plugin.

        Returns True if the plugin was unloaded, False if the plugin wasn't loaded in the first place.

        :type path: str | Path
        :rtype: bool
        """
        path = Path(path)
        file_path = path.resolve()

        # make sure this plugin is actually loaded
        if str(file_path) not in self.plugins:
            return False

        # get the loaded plugin
        plugin = self.plugins[str(file_path)]

        for on_cap_available_hook in plugin.hooks["on_cap_available"]:
            available_hooks = self.cap_hooks["on_available"]
            for cap in on_cap_available_hook.caps:
                cap_cf = cap.casefold()
                available_hooks[cap_cf].remove(on_cap_available_hook)
                if not available_hooks[cap_cf]:
                    del available_hooks[cap_cf]

        for on_cap_ack in plugin.hooks["on_cap_ack"]:
            ack_hooks = self.cap_hooks["on_ack"]
            for cap in on_cap_ack.caps:
                cap_cf = cap.casefold()
                ack_hooks[cap_cf].remove(on_cap_ack)
                if not ack_hooks[cap_cf]:
                    del ack_hooks[cap_cf]

        # unregister commands
        for command_hook in plugin.hooks["command"]:
            for alias in command_hook.aliases:
                if alias in self.commands and self.commands[alias] == command_hook:
                    # we need to make sure that there wasn't a conflict, so we don't delete another plugin's command
                    del self.commands[alias]

        # unregister raw hooks
        for raw_hook in plugin.hooks["irc_raw"]:
            if raw_hook.is_catch_all():
                self.catch_all_triggers.remove(raw_hook)
            else:
                for trigger in raw_hook.triggers:
                    assert trigger in self.raw_triggers  # this can't be not true
                    self.raw_triggers[trigger].remove(raw_hook)
                    if not self.raw_triggers[trigger]:  # if that was the last hook for this trigger
                        del self.raw_triggers[trigger]

        # unregister events
        for event_hook in plugin.hooks["event"]:
            for event_type in event_hook.types:
                assert event_type in self.event_type_hooks  # this can't be not true
                self.event_type_hooks[event_type].remove(event_hook)
                if not self.event_type_hooks[event_type]:  # if that was the last hook for this event type
                    del self.event_type_hooks[event_type]

        # unregister regexps
        for regex_hook in plugin.hooks["regex"]:
            for regex_match in regex_hook.regexes:
                self.regex_hooks.remove((regex_match, regex_hook))

        # unregister sieves
        for sieve_hook in plugin.hooks["sieve"]:
            self.sieves.remove(sieve_hook)

        # unregister connect hooks
        for connect_hook in plugin.hooks["on_connect"]:
            self.connect_hooks.remove(connect_hook)

        for out_hook in plugin.hooks["irc_out"]:
            self.out_sieves.remove(out_hook)

        for post_hook in plugin.hooks["post_hook"]:
            self.hook_hooks["post"].remove(post_hook)

        for perm_hook in plugin.hooks["perm_check"]:
            for perm in perm_hook.perms:
                self.perm_hooks[perm].remove(perm_hook)

        # Run on_stop hooks
        for on_stop_hook in plugin.hooks["on_stop"]:
            event = Event(bot=self.bot, hook=on_stop_hook)
            await self.launch(on_stop_hook, event)

        # unregister databases
        plugin.unregister_tables(self.bot)

        task_count = len(plugin.tasks)
        if task_count > 0:
            logger.debug("Cancelling running tasks in %s", plugin.title)
            for task in plugin.tasks:
                task.cancel()

            logger.info("Cancelled %d tasks from %s", task_count, plugin.title)

        # remove last reference to plugin
        del self.plugins[plugin.file_path]

        if self.bot.config.get("logging", {}).get("show_plugin_loading", True):
            logger.info("Unloaded all plugins from %s", plugin.title)

        return True

    def _log_hook(self, hook):
        """
        Logs registering a given hook

        :type hook: cloudbot.hooks.hook.Hook
        """
        if self.bot.config.get("logging", {}).get("show_plugin_loading", True):
            logger.info("Loaded %s", hook)
            logger.debug("Loaded %r", hook)

    def _execute_hook_threaded(self, hook, event):
        """
        :type hook: cloudbot.hooks.hook.Hook
        :type event: cloudbot.event.Event
        """
        event.prepare_threaded()

        try:
            return call_with_args(hook.function, event)
        finally:
            event.close_threaded()

    async def _execute_hook_sync(self, hook, event):
        """
        :type hook: cloudbot.hooks.hook.Hook
        :type event: cloudbot.event.Event
        """
        await event.prepare()

        try:
            return await call_with_args(hook.function, event)
        finally:
            await event.close()

    async def internal_launch(self, hook, event):
        """
        Launches a hook with the data from [event]
        :param hook: The hook to launch
        :param event: The event providing data for the hook
        :return: a tuple of (ok, result) where ok is a boolean that determines if the hook ran without error and result
            is the result from the hook
        """
        if hook.threaded:
            coro = self.bot.loop.run_in_executor(None, self._execute_hook_threaded, hook, event)
        else:
            coro = self._execute_hook_sync(hook, event)

        task = async_util.wrap_future(coro)
        hook.plugin.tasks.append(task)
        try:
            out = await task
            ok = True
        except Exception:
            logger.exception("Error in hook %s", hook.description)
            ok = False
            out = sys.exc_info()

        hook.plugin.tasks.remove(task)

        return ok, out

    async def _execute_hook(self, hook, event):
        """
        Runs the specific hook with the given bot and event.

        Returns False if the hook errored, True otherwise.

        :type hook: cloudbot.hooks.hook.Hook
        :type event: cloudbot.event.Event
        :rtype: bool
        """
        ok, out = await self.internal_launch(hook, event)
        result, error = None, None
        if ok is True:
            result = out
        else:
            error = out

        post_event = partial(
            PostHookEvent, launched_hook=hook, launched_event=event, bot=event.bot,
            conn=event.conn, result=result, error=error
        )
        for post_hook in self.hook_hooks["post"]:
            success, res = await self.internal_launch(post_hook, post_event(hook=post_hook))
            if success and res is False:
                break

        return ok

    async def _sieve(self, sieve, event, hook):
        """
        :type sieve: cloudbot.hooks.hook.Hook
        :type event: cloudbot.event.Event
        :type hook: cloudbot.hooks.hook.Hook
        :rtype: cloudbot.event.Event
        """
        if sieve.threaded:
            coro = self.bot.loop.run_in_executor(None, sieve.function, self.bot, event, hook)
        else:
            coro = sieve.function(self.bot, event, hook)

        result, error = None, None
        task = async_util.wrap_future(coro)
        sieve.plugin.tasks.append(task)
        try:
            result = await task
        except Exception:
            logger.exception("Error running sieve %s on %s:", sieve.description, hook.description)
            error = sys.exc_info()

        sieve.plugin.tasks.remove(task)

        post_event = partial(
            PostHookEvent, launched_hook=sieve, launched_event=event, bot=event.bot,
            conn=event.conn, result=result, error=error
        )
        for post_hook in self.hook_hooks["post"]:
            success, res = await self.internal_launch(post_hook, post_event(hook=post_hook))
            if success and res is False:
                break

        return result

    async def _start_periodic(self, hook):
        interval = hook.interval
        initial_interval = hook.initial_interval
        await asyncio.sleep(initial_interval)

        while True:
            event = Event(bot=self.bot, hook=hook)
            await self.launch(hook, event)
            await asyncio.sleep(interval)

    async def launch(self, hook, event):
        """
        Dispatch a given event to a given hook using a given bot object.

        Returns False if the hook didn't run successfully, and True if it ran successfully.

        :type event: cloudbot.event.Event | cloudbot.event.CommandEvent
        :type hook: cloudbot.hooks.hook.Hook | cloudbot.hooks.command.CommandHook
        :rtype: bool
        """

        if hook.type not in ("on_start", "on_stop", "periodic"):  # we don't need sieves on on_start hooks.
            for sieve in self.bot.plugin_manager.sieves:
                event = await self._sieve(sieve, event, hook)
                if event is None:
                    return False

        if hook.single_thread:
            # There should only be one running instance of this hook, so let's wait for the last event to be processed
            # before starting this one.

            key = (hook.plugin.title, hook.function_name)
            if key in self._hook_waiting_queues:
                queue = self._hook_waiting_queues[key]
                if queue is None:
                    # there's a hook running, but the queue hasn't been created yet, since there's only one hook
                    queue = asyncio.Queue()
                    self._hook_waiting_queues[key] = queue
                assert isinstance(queue, asyncio.Queue)
                # create a future to represent this task
                future = async_util.create_future(self.bot.loop)
                queue.put_nowait(future)
                # wait until the last task is completed
                await future
            else:
                # set to None to signify that this hook is running, but there's no need to create a full queue
                # in case there are no more hooks that will wait
                self._hook_waiting_queues[key] = None

            # Run the plugin with the message, and wait for it to finish
            result = await self._execute_hook(hook, event)

            queue = self._hook_waiting_queues[key]
            if queue is None or queue.empty():
                # We're the last task in the queue, we can delete it now.
                del self._hook_waiting_queues[key]
            else:
                # set the result for the next task's future, so they can execute
                next_future = await queue.get()
                next_future.set_result(None)
        else:
            # Run the plugin with the message, and wait for it to finish
            result = await self._execute_hook(hook, event)

        # Return the result
        return result
