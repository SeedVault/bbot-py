"""Define a plugin architecture to create bots."""
from __future__ import annotations # see https://www.python.org/dev/peps/pep-0563/
import abc
import os
import importlib
import logging
import logging.config
import datetime
import smokesignal
from box import Box
from logging.config import DictConfigurator
from bbot.config import load_configuration

from typing import Any


class Plugin(metaclass=abc.ABCMeta):
    """Generic plugin."""

    def __init__(self, config: dict) -> None:
        """
        Initialize the plugin.

        :param config: Configuration values for the instance.
        """
        #self.logger = BBotLoggerAdapter(logging.getLogger('core'), self, self, 'core')        


    @staticmethod
    def load_plugin(plugin_settings: dict, dotbot: dict=None, parent: object=None) -> Any:
        """
        Create a new instance of a plugin dynamically.

        This method creates a new instance of a plugin and set its
        internal attributes and dependencies recursively.

        :param plugin_settings: Dictionary with initialization data.
        :return: An instance of the class defined in plugin_settings
        """
        #print('Loading module ' + plugin_settings['plugin_class'])
        plugin = Plugin.get_class_from_fullyqualified(plugin_settings['plugin_class'])(plugin_settings, dotbot)                
        for attr_name in vars(plugin):
            if attr_name in plugin_settings:
                attr_config = plugin_settings[attr_name]
                if not isinstance(attr_config, dict): # single value
                    plugin.__setattr__(attr_name, attr_config)
                elif  "plugin_class" in attr_config:  # single instance
                    plugin.__setattr__(attr_name, Plugin.load_plugin(attr_config, dotbot, plugin))
                else: # many instances
                    instances = {}
                    for key, values in attr_config.items():
                        if "plugin_class" in values:
                            instances[key] = Plugin.load_plugin(values, dotbot, plugin)

                    plugin.__setattr__(attr_name, instances)
        #@TODO we might change this to run directly on _init_ with a callback
        if hasattr(plugin, 'init'):            
            plugin.init(parent)
        return plugin

    @staticmethod
    def get_class_from_fullyqualified(setting_class):
        """
        Parses plugin_class entry from config settings, imports and returns the class
        :param setting_class: Fully qualified classname
        :return: imported class
        """
        parts = setting_class.strip().split(".")
        class_name = parts.pop()
        package_name = ".".join(parts)
        module = importlib.import_module(package_name)
        dynamic_class = getattr(module, class_name)
        return dynamic_class



@Plugin.register
class BBotCore(Plugin, metaclass=abc.ABCMeta):
    """Abstract base class for chatbot engines."""

    SIGNAL_GET_RESPONSE_AFTER = 'get_response_after'
    SIGNAL_GET_RESPONSE_BEFORE = 'get_response_before'
    SIGNAL_CALL_BBOT_FUNCTION_AFTER = 'call_function_after'
    SIGNAL_CALL_BBOT_FUNCTION_BEFORE = 'call_function_before'
    SIGNAL_TEMPLATE_RENDER = 'template_render'

    FNC_RESPONSE_OK = 1
    FNC_RESPONSE_ERROR = 0

    bot_memory_repo = {}

    def __init__(self, config: dict, dotbot: dict) -> None:
        """
        Initialize the chatbot engine.

        :param config: Configuration values for the instance.
        """
        
        self.config = config
        self.dotbot = dotbot

        self.dotdb = None
        self.logger = None
        self.cache = None
        self.extensions = []
        self.pipeline = []

        self.is_fallback = False
        self.user_id = ''
        self.bot_id = ''
        self.org_id = ''
        self.logger_level = ''
        self.request = {}
        self.executed_functions = []
        self.bot = None

        self.functions_map = {}    # Registered template functions

        self.bbot = BBotFunctionsProxy(self)

        smokesignal.clear_all() # this shouldnt be needed. @TODO build the whole obj hierarchy, store it in a static var and decouple request session data

    def init(self, none):
        """
        Initilize BBot core. Load and init chatbot engine
        """

        # Init core
        #self.extensions[0].init(self)      
       
        # Instatiate chatbot engine and initialize
        config_path = os.path.abspath(os.path.dirname(__file__) + "/../instance")
        config = load_configuration(config_path, "BBOT_ENV")
        
        self.bot = Plugin.load_plugin(config["chatbot_engines"][self.dotbot.chatbot_engine['type']], self.dotbot, self)               
        self.logger = BBotLoggerAdapter(logging.getLogger('core'), self, self.bot, 'core')        
        self.bot.core = self

        #self.bot.init()

    def register_function(self, function_name: str, callback: dict):
        """
        Register template custom function mapped to its plugin method.

        :param function_name: .flow function name
        :param callback: callable array class/method of plugin method
        """
        self.functions_map[function_name] = {**self.functions_map.get(function_name, {}), **callback}
        
    def resolve_arg(self, arg: list, f_type: str='R', render: bool=False):
        """
        """        
        return arg

    def get_response(self, request: dict) -> dict:
        """
        Return a response based on the input data.

        :param request: A dictionary with input data.
        :return: A response to the input data.
        """
        
        self.response = self.bot.response
        self.request = request
        self.bot.reset()        
        self.bot.user_id = request.get('user_id', '')
        self.bot.pub_id = request.get('pub_id', '')
        
        try:
            smokesignal.emit(BBotCore.SIGNAL_GET_RESPONSE_BEFORE, {})
            self.bot.get_response(request)      # get bot response                       
            self.process_pipeline()             # run pipelin process: this proces changes bot output            
            smokesignal.emit(BBotCore.SIGNAL_GET_RESPONSE_AFTER, {'bbot_response': self.response})  # triggers event: none of these should modify output
        except BBotCoreHalt as e: # this exception is used to do a soft halt
            self.logger.debug(e)  
        #except BBotCoreError as e: # this exception sends exception msg to the bot output
        #    self.bbot.text(e)
        
        self.logger.debug('Response from bbot metaengine: ' + str(self.response))
        return self.response

    def process_pipeline(self):
        """
        Executes all processes listed in the pipeline configuration
        """
        for p in self.pipeline:            
            self.pipeline[p].process()

    def add_output(self, bbot_output_obj: dict, add_last: bool=False):
        """      
        Appends a new output object
        """
        self.logger.debug('Adding to output: ' + str(bbot_output_obj))
        if not add_last or len(self.response['output']) == 0:
            self.response['output'].append(bbot_output_obj) # append output
        else:
            self.response['output'][-1] = {**self.response['output'][-1], **bbot_output_obj} # add to last output @TODO should check if it's message type
            
    def reset_output(self):
        """
        Resets the output
        """
        self.logger.debug('Resetting output')
        self.response['output'] = []        

    @staticmethod
    def create_bot(config: dict, dotbot: dict={}):
        """
        Create a bot.

        :param config: Configuration settings.
        :param chatbot_engine_name: Name of engine to create. If ommited,
                            defaults to value specified under the key
                            "bbot.default_chatbot_engine" in configuration file.
        :return: Instance of BBotCore class.
        """
                
        print("5")
        if config['bot_caching'] and dotbot.id in BBotCore.bot_memory_repo.keys():
            print('Bot found in memory')
            bbot = BBotCore.bot_memory_repo[dotbot.id]
        else:                        
            print('Loading bot')
            bbot = Plugin.load_plugin(config, dotbot)  

            if config['bot_caching']:
                BBotCore.bot_memory_repo[dotbot.id] = bbot # store in memory 
        
        return bbot

    @staticmethod
    def create_request(
        chan_input: dict, user_id: str, bot_id: str = "", org_id: str = "", pub_id: str = "", channel_id: str = "") -> dict:
        """
        Create a base request.

        :param chan_input: Input from channel
        :param user_id: User ID
        :param bot_id: Bot ID
        :param org_id: Organization ID
        :return: A dictionary with the given data
        """
        return {"user_id": user_id, "bot_id": bot_id, "org_id": org_id, "pub_id": pub_id, "channel_id": channel_id, "input": chan_input}

    @staticmethod
    def create_response(output: dict) -> dict:
        """
        Create a base response.

        :param output: Output from bot engine
        :return: A dictionary with the given data
        """
        return output

    @staticmethod
    def get_all_texts_from_output(bbot_response: dict) -> str:
        """Returns all concatenated texts from a bbot response"""
        texts = ''

        for r in bbot_response:
            response_type = list(r.keys())[0]
            if response_type == 'text':
                texts += r['text'] + '.\n' # @TODO improve this
        return texts

    def extensions_cache(func):
        """
        Decorator to apply cache to extensions
        @TODO add ttl 5min
        """
        def function_wrapper(self, args, f_type):                        
            # key = botid_methodname_arg0_arg1_arg2
            # adds args only if they are string, integer or boolean (avoiding nonhashable values [even in nested values])
            key = self.core.bot_id + "_" + func.__name__
            for arg in args:
                if isinstance(arg, (str, int, bool)):
                    key += "_" + str(arg)
            cached = self.core.cache.get(key)
            if cached is None:
                value = func(self, args, f_type)
                self.core.cache.set(key, value)
                return value
            else:
                self.logger.debug('Found cached value!')
                return cached

        return function_wrapper

    def get_publisher_name(self):
        return self.dotbot.botsubscription.publisher_name
    
    def get_publisher_subscription_type(self):                
        return self.dotbot.botsubscription.subscription_type

@Plugin.register
class ChatbotEngine(Plugin, metaclass=abc.ABCMeta):
    """Abstract base class for chatbot engines."""

    @abc.abstractmethod
    def __init__(self, config: dict, dotbot: dict) -> None:
        """
        Initialize the instance.

        :param config: Configuration values for the instance.
        """
        self.core = None        

        self.dotbot = dotbot
        self.config = config
        self.channel_id = None
        self.logger = None        

        self.reset()
        
        
    @abc.abstractmethod
    def get_response(self, request: dict) -> dict:
        """
        Get response based on the request
        """
        self.reset()
        self.request = request    
        self.user_id = request.get('user_id', '')
        self.pub_id = request.get('pub_id', '')
        self.channel_id = request.get('channel_id')
        
    @abc.abstractmethod
    def init(self, core: BBotCore) -> None:
        """
        Initializes chatbot engine with config and dotbot loaded
        """
        self.core = core
        self.bbot = core.bbot
    
    def reset(self):
        self.user_id = ''
        self.pub_id = ''
        self.channel_id = ''
        self.logger_level = ''
        self.is_fallback = False
        self.bot_id = self.dotbot.bot_id
        self.request = {}  
        self.response = {
            'output': []           
        }

    def add_output(self, bbot_output_obj: dict):
        """
        Adds a BBot output object to the output stream
        @TODO add bbot in/out protocol specification

        :param bbot_output_obj:
        :return:
        """
        self.response['output'].append(bbot_output_obj)


class BBotCoreHalt(Exception):
    """Used by Core to halt the bot when something happens and dont want to keep running with following execution"""

class ChatbotEngineNotFoundError(Exception):
    """ChatbotEngine not found."""


class ChatbotEngineError(Exception):
    """ChatbotEngine error."""


class BBotException(Exception):
    """BBot plugin error."""

    def __init__(self, args):
        self.args = args
        super(BBotException, self).__init__('args: {}'.format(args))

    def __reduce__(self):
        return BBotException, self.args

class BBotExtensionException(Exception):
    """BBot extension error"""  

    def __init__(self, message, code):

        # Call the base class constructor with the parameters it needs
        super().__init__(message)

        # Now for your custom code...
        self.code = code        
      
class ChatbotEngineExtension():
    """Base class for extensions."""
    def __init__(self, config: dict, dotbot: dict) -> None:
        pass


@Plugin.register
class Cache(Plugin, metaclass=abc.ABCMeta):
    """Abstract base class for cache."""

    @abc.abstractmethod
    def __init__(self, config: dict) -> None:
        """
        Initialize the cache.

        :param config: Configuration values for the instance.
        """
        super(Cache, self).__init__(config)


    @abc.abstractmethod
    def set(self) -> None:
        """
        Set cache value
        """
        return ""

    @abc.abstractmethod
    def get(self) ->str:
        """
        Get cached value
        """
        return ""



class BBotLoggerAdapter(logging.LoggerAdapter):
    """
    Custom Logger Adapter to add some context data and more control on DotFlow extensions logging behavior
    """

    def __init__(self, logger, module: object, bot, mod_name=''):
        """

        :param logger:
        :param module:
        :param bot:
        :param mod_name:
        """
        super().__init__(logger, {})

        self.bot = bot
        self.mod_name = mod_name

        if module.logger_level:            
            self.setLevel(module.logger_level)

        """ custom handlers dont work
        if module.config.get('logger_handler'):
            df = DictConfigurator(bot.logging_config)
            handler_config = bot.logging_config['handlers'][module.config['logger_handler']]
            self.logger.addHandler(df.configure_handler(handler_config))
        """

    def process(self, msg, kwargs):
        """

        :param msg:
        :param kwargs:
        :return:
        """        
        # We need to set extras here because we need bot object ref to get user_id when available (it's not available at extension's init)

        bot_id = ''
        try:
            bot_id = self.bot.dotbot['id']
        except (AttributeError, TypeError):        
            pass

        bot_name = ''
        try:
            bot_name = self.bot.dotbot['name']
        except (AttributeError, TypeError):        
            pass

        user_id = ''
        try:
            user_id = self.bot.user_id
        except (AttributeError, TypeError):        
            pass

        user_ip = ''
        try:
            user_ip = self.bot.user_ip
        except (AttributeError, TypeError):        
            pass

        extra = {
            'bot_id': bot_id,
            'bot_name': bot_name,
            'user_id': user_id,
            'user_ip': user_ip
        }

        kwargs["extra"] = extra
        return msg, kwargs


class BBotFunctionsProxy:
    """
    This class is a proxy to call BBot functions in a easy way
    Ex:
    bbot = BBotFunctionsProxy()
    bbot.fname()
    """    
    def __init__(self, core: BBotCore):
        self.core = core

    def __getattr__(self, name):
        def wrapper(*args, **kwargs):   

            try:
                f_type
            except NameError:
                f_type = 'R'
                        
            # define function map woth both bbot and bot engine functions
            self.global_functions_map = self.core.functions_map
            if hasattr(self.core.bot, 'functions_map'):
                self.global_functions_map = {**self.global_functions_map, **self.core.bot.functions_map}
            
            if name in self.global_functions_map: # will throw exception if bot doesnt have functions map
                return self.call_function(name, args, f_type)
            else:
                err_msg = 'Tried to run function "' + name + '" but it\'s not registered'
                self.core.logger.debug(err_msg)            
                raise BBotException(err_msg)
        
        return wrapper

    def call_function(self, func_name: str, args: list, f_type: str='R'):
        """
        Executes a BBot function or any bot engine function listed in functions_map attribute

        :param func_name: Name of the function
        :param args: List with arguments
        :param f_type: Function Type
        :return:
        """        
        self.core.logger.debug('Calling function "' + func_name + '" with args ' + str(args))
        fmap = self.global_functions_map[func_name]

        start = datetime.datetime.now()
        response = None
        resp_code = BBotCore.FNC_RESPONSE_OK
        error_message = ""
        cost = fmap['cost']
        exception = None

        try:
            smokesignal.emit(BBotCore.SIGNAL_CALL_BBOT_FUNCTION_BEFORE, {'name': func_name, 'args': args, 'register_enabled': fmap['register_enabled'], 'data': fmap}) 

            response = getattr(fmap['object'], fmap['method'])(args, f_type)            

            # convert dict into box to gain dotted notation access
            if isinstance(response, dict):
                response = Box(response)

        except BBotExtensionException as e:                
            exception = e # do not remove >> https://stackoverflow.com/questions/29268892/python-3-exception-deletes-variable-in-enclosing-scope-for-unknown-reason
            resp_code = e.code
            error_message = str(e)
            cost = 0
            response = '<error>' #@TODO customize and handle it by environment        
        end = datetime.datetime.now()
        self.core.logger.debug('Function response: ' + str(response))

        # Adds debug information about the executed function
        self.core.executed_functions.append({
            'function': func_name,
            'args': args,
            'return': response,
            'responseTime': int((end - start).total_seconds() * 1000)
        })
                        
        smokesignal.emit(BBotCore.SIGNAL_CALL_BBOT_FUNCTION_AFTER, 
            {
                'name': func_name,                 
                'response_code': resp_code,                 
                'error_message': error_message,
                'register_enabled': fmap['register_enabled'], 
                'data': fmap
            })
        
        if resp_code == BBotCore.FNC_RESPONSE_OK:
            return response
        else:            
            exception.args = (func_name  + ' function error', ) + exception.args
            raise exception # propagate it to stop all functions execution
