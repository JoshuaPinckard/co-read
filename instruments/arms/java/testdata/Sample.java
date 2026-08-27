class Sample {
    static {
        System.setProperty("sample", "true");
    }

    Sample() {
        super();
    }

    void method() {
        System.out.println("method");
    }

    Runnable lambda = () -> System.out.println("lambda");

    Runnable anonymous = new Runnable() {
        @Override
        public void run() {
            System.out.println("anonymous");
        }
    };

    class Inner {
        void nested() {
            System.out.println("nested");
        }
    }
}

interface WithDefault {
    void abstractMethod();

    default void defaultMethod() {
        System.out.println("default");
    }
}

abstract class AbstractSample {
    abstract void withoutBody();
}
